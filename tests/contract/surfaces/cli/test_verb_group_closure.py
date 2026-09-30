"""SURF-080..083: the root groups, the delivery anchors, the create documents, the envelope.

- SURF-080: ``campaign``, ``question`` and ``ui`` are root groups, and the
  old ``research campaign`` / ``research question`` / ``tui`` spellings are
  gone rather than kept beside them.
- SURF-081: every delivery verb and every release verb that mutates a
  record requires ``--expected-revision`` and sends the caller's value, never
  one read off the record it sends.
- SURF-082: ``question add``, ``campaign new``, ``decision add`` and
  ``release create`` read ``--from-spec <path>`` or ``-`` and parse it
  through a closed model before anything is sent.
- SURF-083: the integration, legacy and release verbs answer with the one
  machine envelope, rendered by the shared renderer in both modes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import click
import orjson
import pytest
import typer
from pydantic import BaseModel, ConfigDict
from typer.testing import CliRunner

from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode, DomainStatus
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes, verb_contract
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain_integration as integration_cmd
from eawf.surfaces.cli.commands import domain_legacy as legacy_cmd
from eawf.surfaces.cli.commands import release as release_cmd
from tests._epoch2_helpers import lay_epoch2_tree
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_TASK = f"{_ROOT}/task/CANARY-0001"
_RUN = f"{_ROOT}/run/RUN-00000001"
_BATCH = f"{_ROOT}/batch/BAT-0001"
_MILESTONE = f"{_ROOT}/milestone/MLS-0001"
_RELEASE_KEY = "REL-0.7.0.dev9"


def _root() -> click.Group:
    command = typer.main.get_command(app)
    assert isinstance(command, click.Group)
    return command


def _group(*path: str) -> click.Group:
    group = _root()
    for name in path:
        group = group.commands[name]  # type: ignore[assignment]
        assert isinstance(group, click.Group)
    return group


def _options(*path: str) -> dict[str, click.Parameter]:
    command = _group(*path[:-1]).commands[path[-1]]
    return {opt: param for param in command.params for opt in param.opts}


# ---- SURF-080 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("group", "verbs"),
    [
        ("campaign", {"new", "run", "cancel"}),
        ("question", {"add", "resolve", "list", "open-decision", "answer"}),
    ],
)
def test_surf_080_campaign_and_question_are_root_entity_groups(group: str, verbs: set[str]) -> None:
    assert group in verb_contract.ENTITY_GROUPS
    assert set(_group(group).commands) == verbs


def test_surf_080_the_research_group_keeps_only_its_reads() -> None:
    """The move is clean: no second spelling is left under ``research``."""
    assert not {"campaign", "question"} & set(_group("research").commands)


def test_surf_080_ui_is_the_root_launch_verb_and_tui_is_gone() -> None:
    mounted = set(_root().commands)
    assert "ui" in mounted
    assert "tui" not in mounted


def test_surf_080_a_moved_group_answers_at_its_new_path_only() -> None:
    """Error path: the old path is an unknown command, the new one is the group."""
    old = runner.invoke(app, ["research", "question", "list"])
    assert old.exit_code == click.UsageError("x").exit_code
    assert "No such command 'question'" in old.output
    assert "list" in runner.invoke(app, ["question"]).output


# ---- SURF-081 ---------------------------------------------------------------

#: Every mutating delivery command, which must require the anchor.
_DELIVERY_MUTATORS: tuple[tuple[str, ...], ...] = (
    ("task", "submit"),
    ("task", "seal"),
    ("task", "prove"),
    ("batch", "integrate"),
    ("batch", "adopt-landed"),
    ("batch", "reconcile"),
    ("milestone", "open-approval"),
    ("milestone", "seal-approval"),
    ("record", "evidence"),
)

#: Every release command that mutates a record the caller read.
_RELEASE_MUTATORS: tuple[str, ...] = (
    "observe",
    "publish",
    "retry",
    "reconcile",
    "burn",
    "adopt",
    "cancel",
)


@pytest.mark.parametrize("path", _DELIVERY_MUTATORS, ids=" ".join)
def test_surf_081_every_delivery_mutator_requires_the_anchor(path: tuple[str, ...]) -> None:
    assert _options(*path)["--expected-revision"].required


@pytest.mark.parametrize("verb", _RELEASE_MUTATORS)
def test_surf_081_every_release_mutator_requires_the_anchor(verb: str) -> None:
    assert _options("release", verb)["--expected-revision"].required


def _receipt_answer() -> dict[str, Any]:
    return {
        "evidence_ref": f"{_ROOT}/evidence/EVD-0001",
        "created": True,
    }


def test_surf_081_the_delivery_anchor_reaches_the_wire(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _receipt_answer()
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "record", "evidence", _MILESTONE,
            "--kind", "audit", "--summary", "audit A-01 passed",
            "--expected-revision", "7", "--idempotency-key", "evd-1", "--actor", "OPERATOR",
        ],
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == integration_cmd.DELIVERY_RECORD_EVIDENCE
    assert params["expected_revision"] == 7


def test_surf_081_a_delivery_move_without_the_anchor_sends_nothing(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    """Error path: the verb refuses at argument parsing, before the wire."""
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "task", "prove", _TASK,
            "--idempotency-key", "prove-1", "--actor", "OPERATOR",
        ],
    )  # fmt: skip
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []


def test_surf_081_a_stale_delivery_anchor_is_a_revision_conflict_envelope(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    daemon.error = DaemonRpcError(
        -32002, "validation_failed: revision_conflict: the record is at revision 4 but the "
        "request expects 3",
    )  # fmt: skip
    result = runner.invoke(
        app,
        [
            "--json", "--workspace", str(tmp_path), "batch", "reconcile", _BATCH,
            "--expected-batch-revision", "3", "--idempotency-key", "r-1", "--actor", "OPERATOR",
        ],
    )  # fmt: skip
    assert result.exit_code == exit_codes.STATE_CONFLICT
    [row] = orjson.loads(result.stdout)["errors"]
    assert row["code"] == DomainErrorCode.REVISION_CONFLICT.value
    assert row["guard"] is None


@pytest.fixture
def release_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[str, dict[str, Any]]]:
    """Route the release verbs, run from an epoch-2 tree, into a recorder."""
    monkeypatch.setenv("EA_STATE", str(lay_epoch2_tree(tmp_path / "repo")))
    sent: list[tuple[str, dict[str, Any]]] = []

    def dispatch(method: str, params: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        sent.append((method, params))
        return {"release": {"key": _RELEASE_KEY, "status": "cancelled", "revision": 10}}

    monkeypatch.setattr(release_cmd, "_dispatch", dispatch)
    return sent


def test_surf_081_the_release_anchor_is_the_callers_not_the_records(
    release_calls: list[tuple[str, dict[str, Any]]], tmp_path: Path
) -> None:
    """The file says revision 4; the caller read 9, and 9 is what is sent."""
    record = tmp_path / "release.json"
    record.write_bytes(orjson.dumps({"key": _RELEASE_KEY, "revision": 4}))
    result = runner.invoke(
        app,
        [
            "--json", "release", "cancel", _RELEASE_KEY, "--release", str(record),
            "--reason", "nothing shipped", "--expected-revision", "9",
        ],
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    [(_, params)] = release_calls
    assert params["expected_revision"] == 9
    envelope = orjson.loads(result.stdout)
    assert (envelope["revision_before"], envelope["revision_after"]) == (9, 10)


# ---- SURF-082 ---------------------------------------------------------------


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    blocking: bool = False


def test_surf_082_flags_build_the_request_through_the_model() -> None:
    request = verb_contract.request_document(_Request, None, {"title": "t", "blocking": None})
    assert request == _Request(title="t")


def test_surf_082_a_spec_beside_a_flag_is_refused(tmp_path: Path) -> None:
    spec = tmp_path / "q.json"
    spec.write_bytes(b'{"title": "t"}')
    with pytest.raises(cli_errors.UserError, match="drop title"):
        verb_contract.request_document(_Request, spec, {"title": "t"})


@pytest.mark.parametrize(
    ("document", "field"), [({}, "title"), ({"title": "t", "status": "x"}, "status")]
)
def test_surf_082_a_document_the_model_refuses_names_the_field(
    tmp_path: Path, document: dict[str, Any], field: str
) -> None:
    """Boundary and error path: an empty document and an undeclared field."""
    spec = tmp_path / "q.json"
    spec.write_bytes(orjson.dumps(document))
    with pytest.raises(cli_errors.UserError, match=f"check .*{field}"):
        verb_contract.request_document(_Request, spec, {})


@pytest.fixture
def research_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeDaemon:
    """Point the research verbs at a fake daemon over a bare state root."""
    fake = FakeDaemon()
    fake.result = {"question_id": "OQ-1", "status": "blocked", "scope_id": "CANARY"}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", fake)
    lay_epoch2_tree(tmp_path)
    return fake


def test_surf_082_question_add_reads_the_whole_question_from_stdin(
    research_daemon: FakeDaemon, tmp_path: Path
) -> None:
    document = {"title": "which model fits", "blocking": True, "urgency": "high"}
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "question", "add", "--from-spec", "-"],
        input=json.dumps(document),
    )
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = research_daemon.calls
    assert method == "research.add_question"
    assert {key: params[key] for key in document} == document


def test_surf_082_question_add_refuses_an_undeclared_field_before_the_wire(
    research_daemon: FakeDaemon, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "question", "add", "--from-spec", "-"],
        input=json.dumps({"title": "t", "owner": "someone"}),
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "owner" in result.output
    assert research_daemon.calls == []


def test_surf_082_release_create_reads_its_request_from_stdin(
    release_calls: list[tuple[str, dict[str, Any]]],
) -> None:
    document = {"version": "0.7.0.dev9", "membership_refs": ["bundle://a"]}
    result = runner.invoke(
        app, ["release", "create", "--from-spec", "-"], input=json.dumps(document)
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert release_calls == [(release_cmd.RELEASE_RPC_METHODS["create"], document)]


def test_surf_082_release_create_refuses_a_version_beside_the_spec(
    release_calls: list[tuple[str, dict[str, Any]]], tmp_path: Path
) -> None:
    spec = tmp_path / "create.json"
    spec.write_bytes(b'{"version": "0.7.0.dev9"}')
    result = runner.invoke(app, ["release", "create", "0.7.0.dev9", "--from-spec", str(spec)])
    assert result.exit_code == exit_codes.USER_ERROR
    assert release_calls == []


def test_surf_082_decision_add_refuses_a_document_missing_a_field(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--no-input", "init", "--quick", "--target", str(tmp_path)])
    assert result.exit_code == exit_codes.OK, result.output
    result = runner.invoke(
        app,
        ["-w", str(tmp_path), "decision", "add", "--from-spec", "-"],
        input=json.dumps({"decision_id": "D901", "scope_id": "P01", "summary": "Keep the flag"}),
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "rationale" in result.output


# ---- SURF-083 ---------------------------------------------------------------


def _leaf_values(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        for item in value.values():
            yield from _leaf_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _leaf_values(item)
    elif value is not None:
        yield value if isinstance(value, str) else orjson.dumps(value).decode()


def _prove_args(tmp_path: Path) -> list[str]:
    return [
        "--workspace", str(tmp_path), "task", "prove", _TASK,
        "--expected-task-revision", "2", "--idempotency-key", "prove-1", "--actor", "OPERATOR",
    ]  # fmt: skip


@pytest.mark.parametrize("passed", [True, False], ids=["passed", "not-passed"])
def test_surf_083_an_integration_verb_renders_the_same_facts_in_both_modes(
    daemon: FakeDaemon, tmp_path: Path, passed: bool
) -> None:
    proof = {
        "task_ref": _TASK,
        "legs": [{"gate_id": "G-01", "result": "pass" if passed else "fail"}],
        "passed": passed,
        "reason": "every leg passes" if passed else "a leg failed",
    }
    daemon.result = {
        "operation_ref": "operation://00000000-0000-0000-0000-000000000001",
        "operation": {"state": "succeeded", "result": proof},
        "replayed": False,
    }
    machine = runner.invoke(app, ["--json", *_prove_args(tmp_path), "--wait"])
    human = runner.invoke(app, [*_prove_args(tmp_path), "--wait"])
    assert machine.exit_code == human.exit_code
    assert machine.exit_code == (exit_codes.OK if passed else exit_codes.STATE_CONFLICT)
    payload = orjson.loads(machine.stdout)
    assert payload["result"] == proof
    assert payload["status"] == (DomainStatus.OK if passed else DomainStatus.ERROR).value
    for fact in _leaf_values(payload):
        assert fact in human.stdout, fact


@pytest.mark.parametrize(
    ("message", "code", "guard"),
    [
        ("validation_failed: revision_conflict: stale", DomainErrorCode.REVISION_CONFLICT, None),
        (
            "validation_failed: reconcile_batch_not_merging: BAT-0001 is ACTIVE",
            DomainErrorCode.TRANSITION_GUARD_FAILED,
            "reconcile_batch_not_merging",
        ),
        (
            "validation_failed: recording release verbs require an on-disk state root",
            DomainErrorCode.TRANSITION_GUARD_FAILED,
            None,
        ),
    ],
    ids=["declared", "finer", "codeless"],
)
def test_surf_083_a_raised_refusal_becomes_an_envelope_row(
    message: str, code: DomainErrorCode, guard: str | None
) -> None:
    envelope = verb_contract.refusal_envelope(message, operation="op.x", urn=_BATCH)
    [row] = envelope.errors
    assert (row.code, row.guard) == (code, guard)
    assert row.message == message.removeprefix("validation_failed: ")
    assert verb_contract.envelope_exit_code(envelope) == exit_codes.STATE_CONFLICT


def test_surf_083_a_legacy_verb_prints_through_the_shared_renderer(
    daemon: FakeDaemon, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope

    monkeypatch.setattr(legacy_cmd, "DaemonClient", daemon)

    daemon.result = verb_contract.answer_envelope(
        {"from_status": "RUNNING", "to_status": "COMPLETED"},
        operation=legacy_cmd.LEGACY_ADVANCE,
        urn="legacy:task/P01-W01",
        revision_before=None,
        revision_after=None,
    ).model_dump(mode="json")
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "task", "advance-legacy", "P01-W01",
            "--to", "COMPLETED", "--actor", "OPERATOR", "--reason", "done",
        ],
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    expected = verb_contract.envelope_text(
        DomainEnvelope.model_validate(daemon.result), urn="legacy:task/P01-W01"
    )
    assert result.stdout.rstrip("\n") == expected


def test_surf_083_a_release_refusal_is_an_envelope_with_the_refusal_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("EA_STATE", str(lay_epoch2_tree(tmp_path)))

    def refuse(method: str, _params: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        raise DaemonRpcError(-32002, "validation_failed: stale_release_revision: moved to 5")

    monkeypatch.setattr(release_cmd, "_dispatch", refuse)
    result = runner.invoke(app, ["--json", "release", "advance", _RELEASE_KEY])
    assert result.exit_code == exit_codes.STATE_CONFLICT
    payload = orjson.loads(result.stdout)
    assert payload["operation"] == release_cmd.RELEASE_RPC_METHODS["advance"]
    assert payload["errors"][0]["guard"] == "stale_release_revision"


def test_surf_083_an_advance_links_the_command_that_opens_the_next_rung(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The next step is a machine fact in the links, so both modes carry it."""
    monkeypatch.setenv("EA_STATE", str(lay_epoch2_tree(tmp_path)))
    answer = {"train": {"checkpoints": [{"version": "0.7.0.dev10", "status": "open"}]}}
    monkeypatch.setattr(release_cmd, "_dispatch", lambda *_a, **_k: answer)
    machine = runner.invoke(app, ["--json", "release", "advance", _RELEASE_KEY])
    human = runner.invoke(app, ["release", "advance", _RELEASE_KEY])
    assert orjson.loads(machine.stdout)["links"] == {"next": "eawf release create 0.7.0.dev10"}
    assert "link next: eawf release create 0.7.0.dev10" in human.stdout
