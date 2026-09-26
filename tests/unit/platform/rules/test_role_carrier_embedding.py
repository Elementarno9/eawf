"""Every runtime's agent for a carried role holds that role's rules in its own text.

Gate-fire proof for embedding: the Codex, OpenCode and Claude agent
definitions ``eawf plugin install`` writes into a repository without
``.ea/rules.yaml``, and the dispatch role registry, carry the carrier body
rendered from the builtin rules, which is the text this repository's carrier
holds below its stamp; no agent names a carrier file or preloads one.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

from eawf.platform.rules.carriers import (
    CARRIER_DIRECTORY,
    CARRIER_NAME_PREFIX,
    builtin_carrier_body,
    builtin_carrier_roles,
    carrier_stamp_line,
    carrier_target,
)
from eawf.platform.rules.modules import builtin_rule_modules
from eawf.platform.rules.render import plan_rule_projections, rule_source_present
from eawf.runtime.runtimes.claude.plugin_install import install_plugin as install_claude
from eawf.runtime.runtimes.codex.plugin_install import install_plugin as install_codex
from eawf.runtime.runtimes.opencode.plugin_install import install_plugin as install_opencode
from eawf.surfaces.render.agents import AGENT_REGISTRY, embed_role_rules
from eawf.workflow.agents.specs.roles import ROLE_REGISTRY

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ROLES_MODULE = "eawf.core.roles"


@pytest.fixture(scope="module")
def bare_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A repository with no rule source, every runtime's plugin installed."""
    root = tmp_path_factory.mktemp("bare")
    home = tmp_path_factory.mktemp("home")
    assert not rule_source_present(root)
    install_claude(root, persist_manifest=False)
    install_codex(root, home=home)
    install_opencode(root, home=home, persist_manifest=False)
    return root


def _agents(root: Path) -> dict[str, dict[str, str]]:
    claude = {p.stem: p.read_text("utf-8") for p in (root / ".claude/agents").glob("*.md")}
    codex = {
        p.stem: tomllib.loads(p.read_text("utf-8"))["developer_instructions"]
        for p in (root / ".codex/agents").glob("*.toml")
    }
    opencode = {p.stem: p.read_text("utf-8") for p in (root / ".opencode/agents").glob("*.md")}
    return {"claude": claude, "codex": codex, "opencode": opencode}


def _carried() -> list[str]:
    return [spec.role for spec in AGENT_REGISTRY if spec.role in builtin_carrier_roles()]


# ---- gate-fire proof: every runtime embeds the carrier text -----------------


def test_install_plugin_embeds_the_carrier_body_in_every_runtime_without_a_rule_source(
    bare_repo: Path,
) -> None:
    agents = _agents(bare_repo)
    assert _carried()
    for runtime, texts in agents.items():
        assert set(texts) == {spec.role for spec in AGENT_REGISTRY}, runtime
        for role in _carried():
            body = builtin_carrier_body(role)
            assert body is not None
            assert body.rstrip() in texts[role], (runtime, role)


def test_install_plugin_embeds_every_role_rule_of_the_builtin_roles_module(
    bare_repo: Path,
) -> None:
    agents = _agents(bare_repo)
    records = builtin_rule_modules()[_ROLES_MODULE].records
    assert any(record.force == "must" for record in records)
    for record in records:
        for role in record.scope.roles:
            for runtime, texts in agents.items():
                assert record.instruction in texts[role], (runtime, role, record.rule_id)


def test_install_plugin_names_no_carrier_file_and_preloads_none(bare_repo: Path) -> None:
    assert not (bare_repo / CARRIER_DIRECTORY).joinpath(f"{CARRIER_NAME_PREFIX}executor").exists()
    for runtime, texts in _agents(bare_repo).items():
        for role, text in texts.items():
            assert f"{CARRIER_DIRECTORY}/{CARRIER_NAME_PREFIX}" not in text, (runtime, role)
    for role, text in _agents(bare_repo)["claude"].items():
        meta = yaml.safe_load(text.split("---\n")[1])
        assert "skills" not in meta, role


def test_role_registry_system_prompt_embeds_the_carrier_body() -> None:
    for role, spec in ROLE_REGISTRY.items():
        body = builtin_carrier_body(role.value)
        if body is None:
            assert "# Rules for the" not in spec.system_prompt, role
        else:
            assert body.rstrip() in spec.system_prompt, role


def test_builtin_carrier_body_is_the_text_this_repository_carrier_holds() -> None:
    outputs = dict(plan_rule_projections(_REPO_ROOT).outputs)
    for role in builtin_carrier_roles():
        text = outputs[carrier_target(role)]
        stamp = carrier_stamp_line(text)
        assert stamp is not None
        assert text.split(f"{stamp}\n", 1)[1] == builtin_carrier_body(role), role


# ---- builtin_carrier_body: boundaries ----------------------------------------


@pytest.mark.parametrize("role", ["", "reviewer", "operator", "Executor", "executor ", "nobody"])
def test_builtin_carrier_body_is_none_for_a_role_without_builtin_rules(role: str) -> None:
    assert builtin_carrier_body(role) is None


def test_builtin_carrier_body_holds_only_the_role_rules() -> None:
    executor = builtin_carrier_body("executor")
    auditor = builtin_carrier_body("auditor")
    assert executor is not None
    assert auditor is not None
    assert executor.startswith("# Rules for the executor role\n")
    for record in builtin_rule_modules()[_ROLES_MODULE].records:
        assert (record.instruction in executor) is ("executor" in record.scope.roles)
        assert (record.instruction in auditor) is ("auditor" in record.scope.roles)


def test_builtin_carrier_body_is_cached() -> None:
    assert builtin_carrier_body("executor") is builtin_carrier_body("executor")


# ---- embed_role_rules: boundaries --------------------------------------------


def test_embed_role_rules_places_the_rules_below_the_title() -> None:
    rules = builtin_carrier_body("executor")
    assert rules is not None
    text = embed_role_rules("executor", "# Executor\n\nDo the work.\n")
    assert text == f"# Executor\n\n{rules.rstrip()}\n\nDo the work.\n"


def test_embed_role_rules_single_line_body() -> None:
    rules = builtin_carrier_body("executor")
    assert rules is not None
    assert embed_role_rules("executor", "# Executor") == f"# Executor\n\n{rules.rstrip()}\n"


def test_embed_role_rules_leaves_an_uncarried_body_unchanged() -> None:
    body = "# Reviewer\n\nReview.\n"
    assert embed_role_rules("reviewer", body) is body


def test_embed_role_rules_empty_body() -> None:
    rules = builtin_carrier_body("auditor")
    assert rules is not None
    assert embed_role_rules("reviewer", "") == ""
    assert embed_role_rules("auditor", "") == f"\n\n{rules.rstrip()}\n"


def test_embed_role_rules_rejects_a_non_string_body() -> None:
    with pytest.raises(AttributeError):
        embed_role_rules("executor", None)  # type: ignore[arg-type]
