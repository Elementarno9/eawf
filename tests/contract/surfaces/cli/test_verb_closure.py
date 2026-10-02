"""SURF-080, SURF-081 and SURF-083 held on the live command tree.

- SURF-080: every root entry is an entity group or a declared exception
  with a reason, and the ``cross_cutting`` exceptions are exactly the
  contract's cross-cutting set.
- SURF-081: every verb the effect table classifies as writing requires
  ``--expected-revision`` and ``--idempotency-key``, or is a declared
  exemption that still requires the one anchor its kind names.
- SURF-083: a contract verb's machine mode is the one envelope, and no
  contract handler prints JSON past the shared emitters.
"""

from __future__ import annotations

import ast
import inspect
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Final

import click
import orjson
import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.runtime.daemon.methods.domain_envelope import DomainEnvelope
from eawf.surfaces.cli import output, verb_closure
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS, CROSS_CUTTING_GROUPS, ENTITY_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS

pytestmark = pytest.mark.contract

_ANCHORS: Final = ("--expected-revision", "--idempotency-key")


def _root() -> click.Group:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    return root


def _leaves(command: click.Command, path: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    if isinstance(command, click.Group):
        if path and command.invoke_without_command:
            yield " ".join(path), command
        for name, child in sorted(command.commands.items()):
            yield from _leaves(child, (*path, name))
    else:
        yield " ".join(path), command


@pytest.fixture(scope="module")
def leaves() -> dict[str, click.Command]:
    return dict(_leaves(_root()))


def _undeclared_roots(root: click.Group) -> list[str]:
    declared = set(ENTITY_GROUPS) | {row.name for row in verb_closure.ROOT_ENTRY_EXCEPTIONS}
    return sorted(set(root.commands) - declared)


# ---- SURF-080 ---------------------------------------------------------------


def test_surf_080_every_root_entry_is_an_entity_group_or_a_declared_exception() -> None:
    root = _root()
    assert _undeclared_roots(root) == []
    names = [row.name for row in verb_closure.ROOT_ENTRY_EXCEPTIONS]
    assert sorted(set(names) - set(root.commands)) == [], "exceptions naming no root entry"
    assert len(names) == len(set(names))
    assert sorted(set(names) & set(ENTITY_GROUPS)) == []


def test_surf_080_an_undeclared_root_group_fails_the_census() -> None:
    """Error path: a group mounted outside the contract and the list is caught."""
    root = _root()
    stray = click.Group(name="phase", commands={"open": click.Command("open")})
    widened = click.Group(name="eawf", commands={**root.commands, "phase": stray})
    assert _undeclared_roots(widened) == ["phase"]


def test_surf_080_the_cross_cutting_exceptions_are_the_declared_five_plus_ui_and_reflect() -> None:
    cross = {r.name for r in verb_closure.ROOT_ENTRY_EXCEPTIONS if r.kind == "cross_cutting"}
    assert cross == {"config", "memory", "workspace", "daemon", "migrate", "ui", "reflect"}
    assert set(CROSS_CUTTING_GROUPS) == cross


def test_surf_080_every_other_exception_is_tooling() -> None:
    kinds = {row.kind for row in verb_closure.ROOT_ENTRY_EXCEPTIONS}
    assert kinds == {"cross_cutting", "tooling"}


def test_surf_080_every_exception_states_its_reason() -> None:
    for row in verb_closure.ROOT_ENTRY_EXCEPTIONS:
        assert row.reason.strip(), row.name


def test_surf_080_the_exception_row_is_a_closed_model() -> None:
    with pytest.raises(ValidationError):
        verb_closure.RootEntryException.model_validate(
            {"name": "x", "kind": "cross_cutting", "reason": "y", "extra": 1}
        )
    with pytest.raises(ValidationError):
        verb_closure.RootEntryException(name="x", kind="cross_cutting", reason="")
    with pytest.raises(ValidationError):
        verb_closure.RootEntryException.model_validate({"name": "x", "kind": "any", "reason": "y"})


def test_surf_080_track_carries_only_its_native_lifecycle(leaves: dict[str, click.Command]) -> None:
    """The epoch-1 ``track sync`` recompute is retired from the Track group."""
    assert sorted(p for p in leaves if p.startswith("track ")) == ["track create", "track retire"]


# ---- SURF-081 ---------------------------------------------------------------


def _required_anchors(command: click.Command) -> set[str]:
    return {
        opt
        for param in command.params
        if isinstance(param, click.Option) and param.required
        for opt in param.opts
        if opt in _ANCHORS
    }


def _spelled(command: click.Command) -> set[str]:
    return {opt for param in command.params for opt in getattr(param, "opts", ())}


def _writing(leaves: dict[str, click.Command]) -> dict[str, click.Command]:
    return {
        path: command
        for path, command in leaves.items()
        if CLI_VERB_EFFECTS[path].effect_class != "read"
    }


def test_surf_081_every_leaf_declares_an_effect(leaves: dict[str, click.Command]) -> None:
    assert sorted(set(leaves) - set(CLI_VERB_EFFECTS)) == []
    assert sorted(set(CLI_VERB_EFFECTS) - set(leaves)) == []


def test_surf_081_every_writing_verb_requires_both_anchors_or_is_exempt(
    leaves: dict[str, click.Command],
) -> None:
    exempt = {row.verb: row for row in verb_closure.ANCHOR_EXEMPTIONS}
    missing: list[str] = []
    for path, command in sorted(_writing(leaves).items()):
        required = _required_anchors(command)
        row = exempt.get(path)
        if row is None:
            if required != set(_ANCHORS):
                missing.append(path)
        elif (partial := verb_closure.PARTIAL_ANCHOR.get(row.kind)) is not None:
            anchor, needed = partial
            taken = required if needed else _spelled(command)
            assert anchor in taken, f"{path} ({row.kind}) must take {anchor}"
    assert missing == [], "writing verbs with neither both anchors nor an exemption"


def test_surf_081_every_exemption_names_a_writing_verb_that_lacks_an_anchor(
    leaves: dict[str, click.Command],
) -> None:
    writing = _writing(leaves)
    verbs = [row.verb for row in verb_closure.ANCHOR_EXEMPTIONS]
    assert len(verbs) == len(set(verbs))
    assert sorted(set(verbs) - set(writing)) == [], "exemptions naming no writing verb"
    anchored = sorted(v for v in verbs if _required_anchors(writing[v]) == set(_ANCHORS))
    assert anchored == [], "exemptions for verbs that already require both anchors"
    for row in verb_closure.ANCHOR_EXEMPTIONS:
        assert row.reason.strip(), row.verb


def test_surf_081_the_exemption_row_is_a_closed_model() -> None:
    with pytest.raises(ValidationError):
        verb_closure.AnchorExemption.model_validate({"verb": "x", "kind": "none", "reason": "y"})
    with pytest.raises(ValidationError):
        verb_closure.AnchorExemption(verb="x", kind="wire_pending", reason="")


@pytest.mark.parametrize(
    ("path", "anchors"),
    [
        (("question", "answer"), set(_ANCHORS)),
        (("campaign", "cancel"), {"--expected-revision"}),
    ],
    ids=["question answer", "campaign cancel"],
)
def test_surf_081_the_anchors_are_spelled_by_the_contract(
    leaves: dict[str, click.Command], path: tuple[str, ...], anchors: set[str]
) -> None:
    command = leaves[" ".join(path)]
    assert _required_anchors(command) == anchors
    assert "--revision" not in _spelled(command)


def test_surf_081_campaign_cancel_sends_the_callers_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The view says revision 4; the caller read 9, and 9 is what is sent."""
    from eawf.surfaces.cli.commands import research

    sent: list[tuple[str, dict[str, Any]]] = []

    def answer(method: str, params: dict[str, Any], **_kwargs: object) -> dict[str, Any]:
        sent.append((method, params))
        if method == research.CAMPAIGN_VIEW:
            return {"campaign_ref": "eawf://W/P/R/campaign/CAM-0001", "revision": 4}
        return {"status": "cancelled"}

    monkeypatch.setattr("eawf.surfaces.cli.commands.domain._native_answer", answer)
    result = CliRunner().invoke(
        app,
        [
            "-w", str(tmp_path), "campaign", "cancel", "CAM-0001", "--actor", "OPERATOR",
            "--reason", "superseded", "--expected-revision", "9",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert sent[-1][0] == research.CAMPAIGN_CLOSE
    assert sent[-1][1]["expected_revision"] == 9


def test_surf_081_campaign_cancel_without_the_anchor_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.domain._native_answer",
        lambda method, *_a, **_k: sent.append(method),
    )
    result = CliRunner().invoke(
        app,
        [
            "-w", str(tmp_path), "campaign", "cancel", "CAM-0001", "--actor", "OPERATOR",
            "--reason", "superseded",
        ],
    )  # fmt: skip
    assert result.exit_code == click.UsageError("x").exit_code
    assert sent == []


# ---- SURF-083 ---------------------------------------------------------------


def _machine(path: tuple[str, ...], payload: dict[str, Any]) -> dict[str, Any]:
    """Return what the machine mode prints for *payload* inside the verb at *path*."""
    ctx = click.Context(_root(), info_name="eawf")
    for name in path:
        ctx = click.Context(click.Command(name), parent=ctx, info_name=name)
    with ctx:
        return output.machine_payload(payload)


def test_surf_083_a_contract_verb_answer_becomes_the_envelope() -> None:
    envelope = _machine(("config", "get"), {"key": "ui.theme", "value": "dark"})
    parsed = DomainEnvelope.model_validate(envelope)
    assert parsed.operation == "config get"
    assert parsed.result == {"key": "ui.theme", "value": "dark"}
    assert set(envelope) == set(DomainEnvelope.model_fields)


def test_surf_083_an_envelope_and_a_refusal_pass_through_unchanged() -> None:
    envelope = _machine(("task", "claim"), {"key": "x"})
    assert _machine(("task", "claim"), envelope) == envelope
    refusal = {
        "schema_version": "1.0",
        "error": "UserError",
        "message": "m",
        "exit_code": 1,
        "exit_name": "USER_ERROR",
        "hint": None,
        "data": None,
        "correlation_id": None,
        "protocol_version": None,
    }
    from eawf.surfaces.cli.errors import ErrorEnvelope

    refusal = {key: refusal.get(key) for key in ErrorEnvelope.model_fields}
    assert _machine(("config", "set"), refusal) == refusal


@pytest.mark.parametrize("path", [("status",), ("hook", "run"), ()], ids=["status", "hook", "root"])
def test_surf_083_a_verb_outside_the_contract_groups_prints_its_payload(
    path: tuple[str, ...],
) -> None:
    """Boundary: root exceptions keep their own shape until regrouped."""
    assert _machine(path, {"a": 1}) == {"a": 1}


def test_surf_083_a_cross_cutting_read_prints_the_envelope_end_to_end(tmp_path: Path) -> None:
    registry = tmp_path / "registry.json"
    result = CliRunner().invoke(
        app, ["--json", "workspace", "list", "--registry-path", str(registry)]
    )
    assert result.exit_code == 0, result.output
    envelope = DomainEnvelope.model_validate(orjson.loads(result.stdout))
    assert envelope.operation == "workspace list"
    assert envelope.status.value == "ok"


def _handler_modules(leaves: dict[str, click.Command]) -> set[str]:
    modules: set[str] = set()
    for path, command in leaves.items():
        if path.split(" ", 1)[0] in CONTRACT_GROUPS and command.callback is not None:
            modules.add(inspect.unwrap(command.callback).__module__)
    return modules


_DUMPERS: Final = {("orjson", "dumps"), ("json", "dumps")}
_PRINTERS: Final = {("typer", "echo"), ("click", "echo"), (None, "print")}


def _call_name(node: ast.expr) -> tuple[str | None, str]:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    if isinstance(node, ast.Name):
        return None, node.id
    return None, ""


def _printed_json(source: str) -> list[int]:
    """Return the lines where *source* prints a JSON dump past the shared emitters."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or _call_name(node.func) not in _PRINTERS:
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and _call_name(inner.func) in _DUMPERS:
                lines.append(node.lineno)
    return lines


def test_surf_083_no_contract_handler_prints_json_past_the_shared_emitters(
    leaves: dict[str, click.Command],
) -> None:
    modules = _handler_modules(leaves)
    assert "eawf.surfaces.cli.commands.config" in modules
    offenders = {
        name: lines
        for name in sorted(modules)
        if (lines := _printed_json(Path(sys.modules[name].__file__ or "").read_text()))
    }
    assert offenders == {}


def test_surf_083_the_printed_json_scan_catches_a_bypass() -> None:
    """Error path: the scan reds on a handler that echoes its own dump."""
    source = "import orjson, typer\ndef f(p):\n    typer.echo(orjson.dumps(p).decode())\n"
    assert _printed_json(source) == [3]
    assert _printed_json("def f(p):\n    return p\n") == []


def test_surf_083_emit_prints_the_envelope_in_machine_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = click.Context(_root(), info_name="eawf")
    child = click.Context(click.Command("memory"), parent=ctx, info_name="memory")
    leaf = click.Context(click.Command("list"), parent=child, info_name="list")
    with leaf:
        output.emit_json_or_text({"rows": []}, "no rows", flags=GlobalFlags(json_output=True))
        output.emit_json_or_text({"rows": []}, "no rows", flags=GlobalFlags())
    out = capsys.readouterr().out
    assert out.endswith("\nno rows\n")
    assert orjson.loads(out.removesuffix("no rows\n"))["result"] == {"rows": []}


# ---- SURF-081: the registry verbs pass the key the wire accepts --------------


class _RegistryClient:
    """A daemon client that records every ``registry.update`` it is sent."""

    sent: ClassVar[list[dict[str, Any]]] = []

    def __enter__(self) -> _RegistryClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def registry_update(self, **params: Any) -> dict[str, Any]:
        self.sent.append(params)
        return {}


@pytest.fixture
def registry_wire(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from eawf.surfaces.cli import _daemon_client, _mutation
    from eawf.surfaces.cli.commands import repo

    _RegistryClient.sent = []
    monkeypatch.setattr(repo, "_daemon_proxy_enabled_for_registry", lambda: True)
    monkeypatch.setattr(_mutation, "_daemon_reachable", lambda *_a: True)
    monkeypatch.setattr(_daemon_client, "DaemonClient", _RegistryClient)
    return _RegistryClient.sent


def _repo_tree(tmp_path: Path, code: str) -> Path:
    tree = tmp_path / "Repos" / code.lower()
    (tree / ".ea").mkdir(parents=True)
    (tree / ".ea" / "state.json").write_text(orjson.dumps({"project": {"code": code}}).decode())
    return tree


def test_surf_081_repo_add_sends_the_callers_idempotency_key(
    registry_wire: list[dict[str, Any]], tmp_path: Path
) -> None:
    registry = tmp_path / "registry.json"
    result = CliRunner().invoke(
        app,
        [
            "repo", "add", str(_repo_tree(tmp_path, "ABC")), "--registry-path", str(registry),
            "--idempotency-key", "add-abc-1",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert [(p["operation"], p["idempotency_key"]) for p in registry_wire] == [("add", "add-abc-1")]


def test_surf_081_repo_add_without_a_key_sends_none(
    registry_wire: list[dict[str, Any]], tmp_path: Path
) -> None:
    """Boundary: the key is optional, and an absent key is not invented."""
    registry = tmp_path / "registry.json"
    tree = _repo_tree(tmp_path, "ABC")
    result = CliRunner().invoke(app, ["repo", "add", str(tree), "--registry-path", str(registry)])
    assert result.exit_code == 0, result.output
    assert [p["idempotency_key"] for p in registry_wire] == [None]


def test_surf_081_several_registry_ops_each_get_a_distinct_key(
    registry_wire: list[dict[str, Any]], tmp_path: Path
) -> None:
    from eawf.platform.registry import Registry, RegistryRepoEntry
    from eawf.surfaces.cli.commands import repo

    registry = tmp_path / "registry.json"
    entries = {
        code: RegistryRepoEntry(code=code, path=str(tmp_path / code), title=code)
        for code in ("ABC", "DEF")
    }
    repo._persist_registry_via_daemon(Registry(repos=entries), registry, idempotency_key="k")
    assert sorted(p["idempotency_key"] for p in registry_wire) == ["k:0", "k:1"]
