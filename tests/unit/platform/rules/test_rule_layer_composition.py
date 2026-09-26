"""Unit tests for builtin < workspace < repository rule-layer composition."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.platform.registry.models import Registry, RegistryRepoEntry, WorkspaceRecord
from eawf.platform.registry.workspace import (
    WORKSPACE_NOT_A_MEMBER,
    WORKSPACE_NOT_REGISTERED,
    WorkspaceResolutionError,
    resolve_member_workspace,
)
from eawf.platform.rules import (
    RuleCommittedInputError,
    RuleDuplicateOwnerError,
    RuleIdentityShadowError,
    RuleProtectedError,
    RuleRecord,
    RuleSourceAbsolutePathError,
    RuleSourceIncludeError,
    RuleSourceLayerError,
    RuleSourceLeakError,
    RuleSourceNamespaceError,
    RuleSourceSchemaError,
    RuleSourceTraversalError,
    RuleSupersessionLayerError,
    RuleWorkspaceDigestError,
    RuleWorkspaceMissingError,
    RuleWorkspaceUnresolvedError,
    compile_card_graph,
    compile_committed_records,
    compile_rule_graph,
    compile_rule_records,
    load_rule_layers,
    no_builtin_rules,
    registered_projection_readers,
    require_committed_inputs,
    rule_digest,
    workspace_rule_locator,
)

_KEY = "TEAM"
# Assembled so the literal never appears in source for the path-leak lint.
MACOS_HOME = "/" + "Users" + "/" + "alice"
_CODE = "DEMO"
_BUILTIN_SOURCE = {"kind": "builtin", "locator": "eawf.core", "digest": "sha256:" + "1" * 64}


def _rule(rule_id: str, obligation: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": rule_id,
        "obligation_id": obligation,
        "revision": 1,
        "title": "Write wave commits with a typed subject",
        "zone": "steering",
        "force": "should",
        "effectiveness": "behavioral",
        "instruction": "Start every wave commit subject with a conventional type.",
        "verification": {"method": "review"},
    }
    body.update(overrides)
    return body


def _builtin(name: str, obligation: str, **overrides: Any) -> RuleRecord:
    return RuleRecord.model_validate(
        {**_rule(f"eawf.core.{name}", obligation, **overrides), "source": _BUILTIN_SOURCE}
    )


def _provider(*records: RuleRecord) -> Callable[[tuple[str, ...]], tuple[RuleRecord, ...]]:
    def provide(modules: tuple[str, ...]) -> tuple[RuleRecord, ...]:
        del modules
        return records

    return provide


def _sha(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


@dataclass
class _Fixture:
    home: Path
    repo: Path

    @property
    def workspace_path(self) -> Path:
        return self.home / ".eawf" / "workspaces" / _KEY / "rules.yaml"

    def write_registry(self, *, members: Iterable[str] = (_CODE,), key: str = _KEY) -> None:
        member_set = frozenset(members) or frozenset({"OTHER"})
        registry = Registry(
            repos={
                _CODE: RegistryRepoEntry(code=_CODE, path=str(self.repo)),
                "OTHER": RegistryRepoEntry(code="OTHER", path=str(self.home / "other")),
            },
            workspaces={
                key: WorkspaceRecord(
                    key=key,
                    member_project_codes=member_set,
                    home_project_code=sorted(member_set)[0],
                )
            },
        )
        path = self.home / ".eawf" / "registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(registry.model_dump_json(), encoding="utf-8")

    def write_workspace(self, document: dict[str, Any] | str) -> bytes:
        raw = (
            document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
        ).encode("utf-8")
        self.workspace_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace_path.write_bytes(raw)
        return raw

    def write_repo(
        self,
        rules: list[dict[str, Any]],
        *,
        pin: str | None = None,
        key: str = _KEY,
        modules: list[str] | None = None,
    ) -> None:
        document: dict[str, Any] = {"schema_version": 1, "rules": rules}
        if pin is not None:
            document["workspace"] = {"key": key, "digest": pin}
        if modules is not None:
            document["modules"] = modules
        path = self.repo / ".ea" / "rules.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    def setup(
        self,
        workspace_rules: list[dict[str, Any]],
        repo_rules: list[dict[str, Any]],
    ) -> None:
        self.write_registry()
        raw = self.write_workspace({"schema_version": 1, "rules": workspace_rules})
        self.write_repo(repo_rules, pin=_sha(raw))


@pytest.fixture
def fx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    # Any path that falls back to the real home directory lands on an empty one.
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    return _Fixture(home=home, repo=repo)


_WS_RULE = _rule("workspace.review-scope", "review-scope")
_WS_EXTRA = _rule("workspace.test-names", "test-names")
_REPO_RULE = _rule("repo.commit-prefix", "commit-prefix")


def test_compile_card_graph_workspace_rule_never_reaches_committed_card(fx: _Fixture) -> None:
    fx.setup([_WS_RULE], [_REPO_RULE])
    policy_one = compile_rule_graph(fx.repo, home=fx.home)
    card_one = compile_card_graph(fx.repo)

    fx.setup([_WS_RULE, _WS_EXTRA], [_REPO_RULE])
    policy_two = compile_rule_graph(fx.repo, home=fx.home)
    card_two = compile_card_graph(fx.repo)

    assert policy_one.digest != policy_two.digest
    assert card_one.digest == card_two.digest
    assert [r.record.rule_id for r in card_two.rules] == ["repo.commit-prefix"]
    assert {s.kind for s in card_two.sources} == {"repository"}
    with pytest.raises(
        RuleCommittedInputError, match=r"workspace\.review-scope.*committed projection"
    ):
        compile_committed_records(
            load_rule_layers(fx.repo, home=fx.home).records(),
            modules=(),
            enforcement_refs=frozenset(),
            projection_readers=registered_projection_readers(),
        )


def test_compile_card_graph_matches_policy_when_workspace_is_empty(fx: _Fixture) -> None:
    fx.setup([], [_REPO_RULE])
    assert compile_card_graph(fx.repo).digest == compile_rule_graph(fx.repo, home=fx.home).digest


def test_compile_card_graph_never_reads_the_registry(fx: _Fixture) -> None:
    fx.write_repo([_REPO_RULE], pin="sha256:" + "0" * 64)
    assert len(compile_card_graph(fx.repo).rules) == 1
    with pytest.raises(RuleWorkspaceUnresolvedError, match="registry is unreadable"):
        compile_rule_graph(fx.repo)


def test_compile_rule_graph_composes_three_layers_in_precedence_order(fx: _Fixture) -> None:
    fx.setup([_WS_RULE], [_REPO_RULE])
    builtin = _builtin("deletion", "deletion")
    graph = compile_rule_graph(fx.repo, builtin_rules=_provider(builtin), home=fx.home)
    assert [r.record.source.kind for r in graph.rules] == ["builtin", "workspace", "repository"]
    assert [s.locator for s in graph.sources] == [
        "eawf.core",
        workspace_rule_locator(_KEY),
        ".ea/rules.yaml",
    ]


def test_compile_rule_graph_without_workspace_reference_skips_registry(fx: _Fixture) -> None:
    fx.write_repo([_REPO_RULE])
    graph = compile_rule_graph(fx.repo)
    assert [r.record.rule_id for r in graph.rules] == ["repo.commit-prefix"]


def test_compile_rule_graph_passes_selected_modules_to_builtin_provider(fx: _Fixture) -> None:
    fx.write_repo([], modules=["eawf.core"])
    seen: list[tuple[str, ...]] = []

    def provide(modules: tuple[str, ...]) -> tuple[RuleRecord, ...]:
        seen.append(modules)
        return ()

    compile_rule_graph(fx.repo, builtin_rules=provide)
    assert seen == [("eawf.core",)]


def test_compile_rule_graph_repository_supersedes_workspace_exactly(fx: _Fixture) -> None:
    fx.setup([_WS_RULE], [])
    target = load_rule_layers(fx.repo, home=fx.home).workspace[0]
    claim = {"rule_id": target.rule_id, "revision": 1, "digest": rule_digest(target)}
    fx.setup([_WS_RULE], [_rule("repo.review-scope", "review-scope", supersedes=[claim])])
    graph = compile_rule_graph(fx.repo, home=fx.home)
    assert [r.record.rule_id for r in graph.rules] == ["repo.review-scope"]
    assert graph.supersessions[0].superseded_rule_id == "workspace.review-scope"


def test_compile_rule_graph_workspace_cannot_supersede_repository(fx: _Fixture) -> None:
    fx.write_repo([_REPO_RULE])
    repo_record = load_rule_layers(fx.repo).repository[0]
    claim = {"rule_id": repo_record.rule_id, "revision": 1, "digest": rule_digest(repo_record)}
    fx.setup([_rule("workspace.commit-prefix", "commit-prefix", supersedes=[claim])], [_REPO_RULE])
    with pytest.raises(RuleSupersessionLayerError, match="strictly higher"):
        compile_rule_graph(fx.repo, home=fx.home)


def test_compile_rule_graph_one_owner_across_workspace_and_repository(fx: _Fixture) -> None:
    fx.setup([_rule("workspace.commit-prefix", "commit-prefix")], [_REPO_RULE])
    with pytest.raises(RuleDuplicateOwnerError, match="commit-prefix"):
        compile_rule_graph(fx.repo, home=fx.home)


def test_compile_rule_graph_workspace_cannot_supersede_protected_builtin(fx: _Fixture) -> None:
    builtin = _builtin("deletion", "deletion", zone="constitution", force="must")
    claim = {"rule_id": builtin.rule_id, "revision": 1, "digest": rule_digest(builtin)}
    fx.setup([_rule("workspace.deletion", "deletion", supersedes=[claim])], [])
    with pytest.raises(RuleProtectedError, match="protected"):
        compile_rule_graph(fx.repo, builtin_rules=_provider(builtin), home=fx.home)


def test_compile_rule_graph_workspace_supersedes_unprotected_builtin(fx: _Fixture) -> None:
    builtin = _builtin("method", "method")
    claim = {"rule_id": builtin.rule_id, "revision": 1, "digest": rule_digest(builtin)}
    fx.setup([_rule("workspace.method", "method", supersedes=[claim])], [])
    graph = compile_rule_graph(fx.repo, builtin_rules=_provider(builtin), home=fx.home)
    assert [r.record.rule_id for r in graph.rules] == ["workspace.method"]


def test_load_rule_layers_refuses_provider_record_from_another_layer(fx: _Fixture) -> None:
    fx.write_repo([])
    stray = RuleRecord.model_validate(
        {
            **_rule("workspace.stray", "stray"),
            "source": {"kind": "workspace", "locator": "x", "digest": "sha256:" + "2" * 64},
        }
    )
    with pytest.raises(RuleSourceLayerError, match="builtin layer"):
        load_rule_layers(fx.repo, builtin_rules=_provider(stray))


def test_load_rule_layers_workspace_false_leaves_workspace_empty(fx: _Fixture) -> None:
    fx.setup([_WS_RULE], [_REPO_RULE])
    layers = load_rule_layers(fx.repo, workspace=False, home=fx.home)
    assert layers.workspace == ()
    assert layers.records() == layers.committed_records()


@pytest.mark.parametrize(
    ("members", "key", "code"),
    [
        (("OTHER",), _KEY, "workspace_not_a_member"),
        ((_CODE,), "ELSE", "workspace_not_registered"),
    ],
)
def test_compile_rule_graph_refuses_unresolved_workspace(
    fx: _Fixture, members: tuple[str, ...], key: str, code: str
) -> None:
    fx.write_registry(members=members, key=key)
    raw = fx.write_workspace({"schema_version": 1, "rules": []})
    fx.write_repo([], pin=_sha(raw))
    with pytest.raises(RuleWorkspaceUnresolvedError, match=code):
        compile_rule_graph(fx.repo, home=fx.home)


def test_compile_rule_graph_refuses_missing_workspace_source(fx: _Fixture) -> None:
    fx.write_registry()
    fx.write_repo([], pin="sha256:" + "0" * 64)
    with pytest.raises(RuleWorkspaceMissingError, match=r"workspaces/TEAM/rules\.yaml"):
        compile_rule_graph(fx.repo, home=fx.home)


def test_compile_rule_graph_refuses_workspace_digest_drift(fx: _Fixture) -> None:
    fx.setup([_WS_RULE], [])
    fx.workspace_path.write_bytes(fx.workspace_path.read_bytes() + b"\n")
    with pytest.raises(RuleWorkspaceDigestError, match="re-pin"):
        compile_rule_graph(fx.repo, home=fx.home)


def _pinned(fx: _Fixture, document: dict[str, Any] | str) -> None:
    fx.write_registry()
    fx.write_repo([], pin=_sha(fx.write_workspace(document)))


@pytest.mark.parametrize(
    ("rule_id", "error"),
    [
        ("eawf.core.deletion", RuleSourceNamespaceError),
        ("repo.deletion", RuleSourceNamespaceError),
    ],
)
def test_load_workspace_rules_refuses_foreign_namespace(
    fx: _Fixture, rule_id: str, error: type[Exception]
) -> None:
    _pinned(fx, {"schema_version": 1, "rules": [_rule(rule_id, "deletion")]})
    with pytest.raises(error, match="'workspace'"):
        compile_rule_graph(fx.repo, home=fx.home)


def test_compile_rule_records_refuses_workspace_record_in_builtin_namespace() -> None:
    shadow = RuleRecord.model_validate(
        {
            **_rule("eawf.core.deletion", "deletion"),
            "source": {"kind": "workspace", "locator": "x", "digest": "sha256:" + "2" * 64},
        }
    )
    with pytest.raises(RuleIdentityShadowError, match="reserved for builtin"):
        compile_rule_records(
            [shadow],
            modules=(),
            enforcement_refs=frozenset(),
            projection_readers=registered_projection_readers(),
        )


@pytest.mark.parametrize(
    ("document", "error", "match"),
    [
        ({"schema_version": 1, "modules": ["eawf.core"]}, RuleSourceSchemaError, "modules"),
        ({"schema_version": 2}, RuleSourceSchemaError, "schema_version"),
        ("- not a mapping\n", RuleSourceSchemaError, "mapping"),
        ({"schema_version": 1, "include": "x.yaml"}, RuleSourceIncludeError, "include"),
        (
            {
                "schema_version": 1,
                "rules": [_rule("workspace.x", "x", rationale=f"{MACOS_HOME}/x")],
            },
            RuleSourceLeakError,
            "home_path",
        ),
        (
            {"schema_version": 1, "rules": [_rule("workspace.x", "x", scope={"paths": ["/etc"]})]},
            RuleSourceAbsolutePathError,
            "absolute",
        ),
        (
            {
                "schema_version": 1,
                "rules": [_rule("workspace.x", "x", procedure_ref="docs/../../x.md")],
            },
            RuleSourceTraversalError,
            "traversal",
        ),
    ],
)
def test_load_workspace_rules_refuses_invalid_source(
    fx: _Fixture, document: dict[str, Any] | str, error: type[Exception], match: str
) -> None:
    _pinned(fx, document)
    with pytest.raises(error, match=match):
        compile_rule_graph(fx.repo, home=fx.home)


def test_load_workspace_rules_accepts_registered_locator_procedure_ref(fx: _Fixture) -> None:
    _pinned(fx, {"schema_version": 1, "rules": [_rule("workspace.x", "x", procedure_ref="ref:a")]})
    assert len(compile_rule_graph(fx.repo, home=fx.home).rules) == 1


@pytest.mark.parametrize("key", ["team", "T", "A" * 17])
def test_load_rule_source_refuses_malformed_workspace_key(fx: _Fixture, key: str) -> None:
    fx.write_repo([], pin="sha256:" + "0" * 64, key=key)
    with pytest.raises(RuleSourceSchemaError, match=r"workspace\.key"):
        compile_card_graph(fx.repo)


def test_require_committed_inputs_boundaries() -> None:
    assert require_committed_inputs([]) == ()
    builtin = _builtin("deletion", "deletion")
    assert require_committed_inputs(iter([builtin])) == (builtin,)


def test_no_builtin_rules_is_empty_for_any_selection() -> None:
    assert no_builtin_rules(()) == ()
    assert no_builtin_rules(("eawf.core",)) == ()


def test_workspace_rule_locator_is_home_relative() -> None:
    assert workspace_rule_locator(_KEY) == "workspaces/TEAM/rules.yaml"


def test_resolve_member_workspace_membership(tmp_path: Path) -> None:
    registry = Registry(
        repos={_CODE: RegistryRepoEntry(code=_CODE, path=str(tmp_path))},
        workspaces={
            _KEY: WorkspaceRecord(
                key=_KEY, member_project_codes=frozenset({_CODE}), home_project_code=_CODE
            )
        },
    )
    assert resolve_member_workspace(registry, key=_KEY, repo_root=tmp_path).key == _KEY
    with pytest.raises(WorkspaceResolutionError) as not_member:
        resolve_member_workspace(registry, key=_KEY, repo_root=tmp_path / "elsewhere")
    assert not_member.value.code == WORKSPACE_NOT_A_MEMBER
    with pytest.raises(WorkspaceResolutionError) as unregistered:
        resolve_member_workspace(registry, key="ELSE", repo_root=tmp_path)
    assert unregistered.value.code == WORKSPACE_NOT_REGISTERED
