"""SURF-080..084 and SURF-086: the verb contract the entity groups answer under.

- SURF-080: the group set is closed, and an entity group's lifecycle moves
  are exactly the daemon's registered lifecycle verbs for that entity.
- SURF-081: every native mutating verb requires ``--expected-revision`` and
  ``--idempotency-key`` and sends nothing without them.
- SURF-082: every native create verb reads ``--from-spec <path>`` or ``-``
  for stdin and parses the document through the kind's strict model.
- SURF-083: the machine and human renderings of an envelope carry the same
  facts.
- SURF-084: a publication submits and answers with its operation reference;
  exit zero means accepted.
- SURF-086: read verbs leave the tree and the home directory byte-identical.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import click
import orjson
import pytest
import typer
from typer.testing import CliRunner

from eawf.runtime.daemon.epoch2_transaction import (
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_VERBS
from eawf.runtime.daemon.methods.domain_envelope import (
    DomainErrorCode,
    accepted_envelope,
    refused_envelope,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes, verb_contract
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_CREATE_DOCUMENTS = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2" / "create"

#: The modules whose commands answer with the native domain envelope.
_ENVELOPE_MODULES = frozenset(
    {
        "eawf.surfaces.cli.commands.domain",
        "eawf.surfaces.cli.commands.domain_delivery",
    }
)

#: Native mutating verbs outside the envelope modules that carry a
#: compare-and-swap anchor on their RPC.
_ANCHORED_ELSEWHERE = frozenset(
    {("milestone", "seal-approval"), ("plan", "approve"), ("plan", "apply")}
)

#: Every create command and the URN it admits, one per kind.
_CREATES: tuple[tuple[str, str, str], ...] = (
    ("track", f"{_ROOT}/track/TRK-CANARY", domain_cmd.TRACK_CREATE),
    ("milestone", f"{_ROOT}/milestone/MLS-0001", domain_cmd.MILESTONE_CREATE),
    ("batch", f"{_ROOT}/batch/BAT-0001", domain_cmd.BATCH_CREATE),
    ("task", f"{_ROOT}/task/CANARY-0001", domain_cmd.TASK_CREATE),
    ("run", f"{_ROOT}/run/RUN-00000001", domain_cmd.RUN_CREATE),
    ("repository", f"{_ROOT}/repository/REP-CANARY", domain_cmd.REPOSITORY_CREATE),
)


def _root() -> click.Group:
    command = typer.main.get_command(app)
    assert isinstance(command, click.Group)
    return command


def _leaves(
    group: click.Group, path: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], click.Command]]:
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            yield from _leaves(command, (*path, name))
        else:
            yield (*path, name), command


def _module(command: click.Command) -> str:
    assert command.callback is not None
    return command.callback.__module__


def _envelope_commands() -> list[tuple[tuple[str, ...], click.Command]]:
    return [(path, cmd) for path, cmd in _leaves(_root()) if _module(cmd) in _ENVELOPE_MODULES]


def _document(kind: str) -> dict[str, Any]:
    document = orjson.loads((_CREATE_DOCUMENTS / f"{kind}.json").read_bytes())
    assert isinstance(document, dict)
    return document


def _receipt(method: str, urn: str, *, before: int | None, after: int) -> MutationReceipt:
    return MutationReceipt(
        event_name=method,
        entity_ref=urn,
        revision_before=before,
        revision_after=after,
        canonical_sequence=7,
        event_id="evt-0007",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0007",
    )


def _create_args(kind: str, urn: str, spec: str) -> list[str]:
    return [
        kind,
        "create",
        urn,
        "--expected-revision",
        "0",
        "--idempotency-key",
        "key-0001",
        "--actor",
        "OPERATOR",
        "--from-spec",
        spec,
    ]


# ---- SURF-080 ---------------------------------------------------------------


def test_surf_080_the_group_set_is_the_closed_amended_set() -> None:
    """Ten entity groups and seven cross-cutting groups, none named twice."""
    assert verb_contract.ENTITY_GROUPS == (
        "track",
        "milestone",
        "batch",
        "task",
        "run",
        "release",
        "campaign",
        "question",
        "action",
        "decision",
    )
    assert verb_contract.CROSS_CUTTING_GROUPS == (
        "workspace",
        "config",
        "daemon",
        "memory",
        "ui",
        "migrate",
        "reflect",
    )
    together = verb_contract.ENTITY_GROUPS + verb_contract.CROSS_CUTTING_GROUPS
    assert len(together) == len(set(together))


def test_surf_080_lifecycle_moves_are_the_daemons_lifecycle_verbs() -> None:
    """Each entity group's moves derive from the registry, in both directions."""
    registered = {
        (verb.kind.value, verb.method.rsplit(".", 1)[1].replace("_", "-"))
        for verb in DOMAIN_LIFECYCLE_VERBS
    }
    exposed = {path for path, _ in _envelope_commands() if path[-1] != "create"}
    assert exposed == registered


def test_surf_080_every_lifecycle_group_is_an_entity_group() -> None:
    """No registered lifecycle entity lives outside the declared entity groups."""
    kinds = {verb.kind.value for verb in DOMAIN_LIFECYCLE_VERBS}
    assert kinds <= set(verb_contract.ENTITY_GROUPS)


def test_surf_080_unmounted_groups_are_the_known_gap() -> None:
    """Pin the declared groups not yet mounted at the root, so a new mount is seen.

    ``action`` has no verb: a pending action is answered through the verb of
    the entity it gates (``milestone seal-approval``), so there is no operation
    of its own to mount.
    """
    mounted = set(_root().commands)
    declared = verb_contract.ENTITY_GROUPS + verb_contract.CROSS_CUTTING_GROUPS
    # ``decision`` has no CLI verb since the flag day retired the epoch-1 decision
    # verbs; a Decision is opened through ``question open-decision``.
    assert {group for group in declared if group not in mounted} == {"action", "decision"}


# ---- SURF-081 ---------------------------------------------------------------


def _anchored_commands() -> list[tuple[tuple[str, ...], click.Command]]:
    return [
        (path, cmd)
        for path, cmd in _leaves(_root())
        if _module(cmd) in _ENVELOPE_MODULES or path in _ANCHORED_ELSEWHERE
    ]


def test_surf_081_every_native_mutating_verb_requires_both_anchors() -> None:
    """``--expected-revision`` and ``--idempotency-key`` are required options."""
    commands = _anchored_commands()
    assert len(commands) == len(DOMAIN_LIFECYCLE_VERBS) + len(_CREATES) + len(_ANCHORED_ELSEWHERE)
    for path, command in commands:
        options = {opt: param for param in command.params for opt in param.opts}
        for flag in ("--expected-revision", "--idempotency-key"):
            assert flag in options, (path, flag)
            assert options[flag].required, (path, flag)


@pytest.mark.parametrize("missing", ["--expected-revision", "--idempotency-key"])
def test_surf_081_a_move_without_an_anchor_sends_nothing(
    daemon: FakeDaemon, tmp_path: Path, missing: str
) -> None:
    """Error path: the verb refuses at argument parsing, before the wire."""
    args = {
        "--expected-revision": "3",
        "--idempotency-key": "key-0001",
        "--actor": "OPERATOR",
    }
    del args[missing]
    flat = [item for pair in args.items() for item in pair]
    urn = f"{_ROOT}/batch/BAT-0001"
    result = runner.invoke(app, ["--workspace", str(tmp_path), "batch", "ready", urn, *flat])
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []


def test_surf_081_the_anchor_reaches_the_wire_as_the_rpc_names_it(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    """The unified flag carries the compare-and-swap token the RPC declares."""
    urn = f"{_ROOT}/run/RUN-00000001"
    daemon.result = accepted_envelope(
        _receipt(domain_cmd.RUN_START, urn, before=5, after=6), operation=domain_cmd.RUN_START
    ).model_dump(mode="json")
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "run",
            "start",
            urn,
            "--expected-revision",
            "5",
            "--idempotency-key",
            "key-0005",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == domain_cmd.RUN_START
    assert params["expected_revision"] == 5
    assert params["idempotency_key"] == "key-0005"


# ---- SURF-082 ---------------------------------------------------------------


@pytest.mark.parametrize(("kind", "urn", "method"), _CREATES)
def test_surf_082_every_create_verb_reads_its_document_from_stdin(
    daemon: FakeDaemon, tmp_path: Path, kind: str, urn: str, method: str
) -> None:
    """``--from-spec -`` reads stdin and forwards the parsed document whole."""
    document = _document(kind)
    daemon.result = accepted_envelope(
        _receipt(method, urn, before=None, after=1), operation=method
    ).model_dump(mode="json")
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), *_create_args(kind, urn, "-")],
        input=orjson.dumps(document).decode(),
    )
    assert result.exit_code == exit_codes.OK, result.output
    [(sent, params)] = daemon.calls
    assert sent == method
    assert params["spec"] == document


@pytest.mark.parametrize(("kind", "urn", "method"), _CREATES)
def test_surf_082_a_document_the_strict_model_refuses_never_reaches_the_wire(
    daemon: FakeDaemon, tmp_path: Path, kind: str, urn: str, method: str
) -> None:
    """Boundary: an empty document is refused as the daemon refuses it."""
    spec = tmp_path / "empty.json"
    spec.write_bytes(b"{}")
    result = runner.invoke(
        app, ["--json", "--workspace", str(tmp_path), *_create_args(kind, urn, str(spec))]
    )
    assert result.exit_code == exit_codes.STATE_CONFLICT, result.output
    assert daemon.calls == []
    payload = orjson.loads(result.stdout)
    assert payload["operation"] == method
    [row] = payload["errors"]
    assert row["code"] == DomainErrorCode.SCHEMA_VALIDATION_FAILED.value
    assert row["message"].startswith(f"the {kind} create document does not validate; check ")
    assert "key" in row["message"]


def test_surf_082_an_extra_field_is_refused_by_the_closed_model(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    """Error path: a field the model does not declare names itself in the refusal."""
    document = {**_document("task"), "status": "COMPLETED"}
    urn = f"{_ROOT}/task/CANARY-0001"
    result = runner.invoke(
        app,
        ["--json", "--workspace", str(tmp_path), *_create_args("task", urn, "-")],
        input=orjson.dumps(document).decode(),
    )
    assert result.exit_code == exit_codes.STATE_CONFLICT, result.output
    assert daemon.calls == []
    assert "status" in orjson.loads(result.stdout)["errors"][0]["message"]


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '"text"'])
def test_surf_082_stdin_that_is_not_a_json_object_is_a_user_error(
    daemon: FakeDaemon, tmp_path: Path, stdin: str
) -> None:
    """Boundary: empty, malformed or non-object stdin stops before the wire."""
    urn = f"{_ROOT}/track/TRK-CANARY"
    result = runner.invoke(
        app, ["--workspace", str(tmp_path), *_create_args("track", urn, "-")], input=stdin
    )
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert daemon.calls == []


def test_surf_082_stdin_at_a_terminal_is_refused_rather_than_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error path: ``-`` with nothing piped names the fix instead of hanging."""

    class _Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Terminal())
    with pytest.raises(cli_errors.UserError, match="pipe it in"):
        verb_contract.read_spec_document(Path("-"))


def test_surf_082_a_missing_spec_file_is_a_user_error(tmp_path: Path) -> None:
    """Error path: an unreadable path is named in the refusal."""
    with pytest.raises(cli_errors.UserError, match="cannot read --from-spec"):
        verb_contract.read_spec_document(tmp_path / "absent.json")


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


def _ok_envelope(urn: str) -> dict[str, Any]:
    envelope = accepted_envelope(
        _receipt(domain_cmd.TASK_PROMOTE, urn, before=2, after=3),
        operation=domain_cmd.TASK_PROMOTE,
        warnings=("projection_degraded",),
    )
    return envelope.model_copy(update={"links": {"task": urn}}).model_dump(mode="json")


def _refused_envelope(urn: str) -> dict[str, Any]:
    refusal = TransactionRefusedError(
        code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
        detail="the task contract is incomplete",
        entity_ref=urn,
        guard="manifest_complete",
        remediation="Complete the contract and retry.",
        revision=2,
    )
    return refused_envelope(refusal, operation=domain_cmd.TASK_PROMOTE).model_dump(mode="json")


@pytest.mark.parametrize("answer", [_ok_envelope, _refused_envelope], ids=["ok", "refused"])
def test_surf_083_the_two_modes_render_the_same_facts(
    daemon: FakeDaemon, tmp_path: Path, answer: Any
) -> None:
    """Every machine fact is in the text, and every text field is a machine fact."""
    urn = f"{_ROOT}/task/CANARY-0001"
    daemon.result = answer(urn)
    args = [
        "--workspace",
        str(tmp_path),
        "task",
        "promote",
        urn,
        "--expected-revision",
        "2",
        "--idempotency-key",
        "key-0001",
        "--actor",
        "OPERATOR",
    ]
    machine = runner.invoke(app, ["--json", *args])
    human = runner.invoke(app, args)
    assert machine.exit_code == human.exit_code
    payload = orjson.loads(machine.stdout)
    assert payload == daemon.result
    text = human.stdout
    for fact in _leaf_values(payload):
        assert fact in text, fact
    facts = set(_leaf_values(payload))
    for line in text.splitlines():
        if ": " in line:
            assert line.split(": ", 1)[1] in facts, line


def test_surf_083_an_envelope_with_no_result_or_links_renders_only_its_facts() -> None:
    """Boundary: nothing is printed for a field the envelope leaves empty."""
    from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope

    envelope = DomainEnvelope.model_validate(_refused_envelope(f"{_ROOT}/task/CANARY-0001"))
    text = verb_contract.envelope_text(envelope, urn=f"{_ROOT}/task/CANARY-0001")
    assert "result." not in text
    assert "link " not in text
    assert text.splitlines()[-1] == "  schema_version: 1"


# ---- SURF-084 ---------------------------------------------------------------


def test_surf_084_a_publication_answers_with_its_operation_reference_on_submission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exit zero means the episode was accepted, while its legs are still queued."""
    from eawf.surfaces.cli.commands import release as release_cmd

    submitted = {
        "operation_ref": "op://release/REL-0.7.0.dev9/1",
        "replayed": False,
        "release": {"key": "REL-0.7.0.dev9", "status": "publishing", "revision": 4},
        "operation": {
            "publication_receipts": [{"target_id": "pypi", "attempt": 1, "status": "queued"}]
        },
    }
    sent: list[str] = []
    monkeypatch.setattr(
        release_cmd, "_current_record", lambda *_a, **_k: {"key": "REL-0.7.0.dev9", "revision": 3}
    )
    monkeypatch.setattr(
        release_cmd, "_dispatch", lambda method, _params, **_k: sent.append(method) or submitted
    )
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "release",
            "publish",
            "REL-0.7.0.dev9",
            "--approved-manifest-digest",
            "sha256:" + "a" * 64,
            "--proof-digest",
            "sha256:" + "b" * 64,
            "--expected-revision",
            "3",
            "--idempotency-key",
            "publish-0001",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert sent == [release_cmd.RELEASE_RPC_METHODS["publish"]]
    assert "result.operation_ref: op://release/REL-0.7.0.dev9/1" in result.stdout
    assert '"target_id":"pypi","attempt":1,"status":"queued"' in result.stdout
    assert "revision 3 -> 4" in result.stdout


# ---- SURF-086 ---------------------------------------------------------------

#: Read verbs of the declared groups that answer from the tree without a daemon.
#: ``daemon status`` and ``follow`` are absent: they answer only from a running
#: daemon and exit with the daemon-unreachable code without one, never starting
#: it; ``tests/integration/surfaces/cli/test_operation_cli.py`` pins that.
_READ_VERBS: tuple[tuple[str, ...], ...] = (
    ("config", "get", "ui.theme"),
    ("config", "validate"),
    ("memory", "digest"),
    ("memory", "list"),
    ("memory", "stale"),
    ("migrate", "status"),
    ("workspace", "list"),
)


def _digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


@pytest.fixture(scope="module")
def _initialised(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, Path]]:
    home = tmp_path_factory.mktemp("home")
    repo = tmp_path_factory.mktemp("repo")
    patch = pytest.MonkeyPatch()
    patch.setenv("HOME", str(home))
    patch.setenv("EAWF_DAEMONLESS", "1")
    result = runner.invoke(app, ["--no-input", "init", "--quick", "--target", str(repo)])
    assert result.exit_code == exit_codes.OK, result.output
    yield repo, home
    patch.undo()


@pytest.mark.parametrize("verb", _READ_VERBS, ids=" ".join)
def test_surf_086_a_read_verb_leaves_the_tree_and_home_byte_identical(
    _initialised: tuple[Path, Path], verb: tuple[str, ...]
) -> None:
    """A successful read writes nothing: no row, no cache, no runtime file."""
    repo, home = _initialised
    before = (_digest(repo), _digest(home))
    result = runner.invoke(app, ["--json", "--workspace", str(repo), *verb])
    assert result.exit_code == exit_codes.OK, result.output
    assert (_digest(repo), _digest(home)) == before


def test_surf_086_the_digest_sees_a_write(tmp_path: Path) -> None:
    """The comparison fires on a one-byte change, so a clean result is meaningful."""
    (tmp_path / ".ea").mkdir()
    (tmp_path / ".ea" / "state.json").write_bytes(b"{}")
    before = _digest(tmp_path)
    (tmp_path / ".ea" / "state.json").write_bytes(b"{ }")
    assert _digest(tmp_path) != before
