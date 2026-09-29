"""The rule graph steers interactive sessions only and never a managed Run.

RULE-080: the rule graph has one audience, a direct interactive harness
session; no rule addresses a managed run and no projection feeds a capsule.
RULE-081: editing a rule's prose, rationale or projection, or deleting every
generated projection, leaves the run-policy digest unchanged.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from pydantic import ValidationError

from eawf.kernel.config.providers import load_provider_configuration
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.platform.rules.generations import GENERATION_STORE_PATH
from eawf.platform.rules.records import AuthoredRule, RuleAudience
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    render_rule_projections,
)
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx


def _rule(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": "repo.release-notes",
        "obligation_id": "demo.release-notes",
        "revision": 1,
        "title": "Keep release notes in the changelog",
        "zone": "constitution",
        "force": "must",
        "effectiveness": "behavioral",
        "instruction": "Keep the release notes in the changelog under the version heading.",
        "rationale": "One place to read what shipped.",
        "verification": {"method": "review"},
    }
    body.update(overrides)
    return body


def _write_rules(root: Path, **overrides: Any) -> None:
    path = root / ".ea" / "rules.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "modules": [], "rules": [_rule(**overrides)]}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _write_rules(root)
    config = {"schema_version": "1.0", "runtime": fx.documents()["workspace"]}
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def _run_policy(root: Path) -> CompiledRunSpec:
    configuration = load_provider_configuration(
        workspace=root, repository=None, registry=fx.registry()
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=configuration,
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


# ---- RULE-080: one audience ------------------------------------------------


def test_rule_080_the_only_audience_is_the_interactive_harness() -> None:
    assert get_args(RuleAudience) == ("interactive_harness",)


@pytest.mark.parametrize("audience", ["managed_run", "capsule", "unattended", ""])
def test_rule_080_a_rule_cannot_address_a_managed_run(audience: str) -> None:
    with pytest.raises(ValidationError, match="audience"):
        AuthoredRule.model_validate(_rule(audience=audience))


def test_rule_080_no_compile_input_carries_a_projection() -> None:
    assert set(inspect.signature(compile_run_spec).parameters) == {
        "request",
        "configuration",
        "bindings",
        "compiled_at",
        "policy_overlays",
    }
    fields = set(CompiledRunSpec.model_fields)
    assert not {name for name in fields if "rule" in name or "projection" in name}


# ---- RULE-081: the run-policy digest ignores the rule graph ----------------


def test_rule_081_a_rule_prose_or_rationale_edit_leaves_the_policy_digest(repo: Path) -> None:
    render_rule_projections(repo)
    before = _run_policy(repo)
    _write_rules(
        repo,
        instruction="Record every shipped change under its version heading in the changelog.",
        rationale="Readers look in one place.",
    )
    render_rule_projections(repo)
    after = _run_policy(repo)
    assert after.policy_digest == before.policy_digest
    assert after.contract_digest == before.contract_digest


def test_rule_081_a_projection_edit_leaves_the_policy_digest(repo: Path) -> None:
    render_rule_projections(repo)
    before = _run_policy(repo).policy_digest
    for name in (CARD_TARGET, POLICY_TARGET):
        path = repo / name
        path.write_text(path.read_text(encoding="utf-8") + "- Push to main.\n", "utf-8")
    assert _run_policy(repo).policy_digest == before


def test_rule_081_deleting_every_generated_projection_leaves_the_policy_digest(
    repo: Path,
) -> None:
    written = render_rule_projections(repo)
    before = _run_policy(repo).policy_digest
    for target in written.manifest.targets:
        (repo / target).unlink()
    (repo / PROJECTION_MANIFEST_PATH).unlink()
    for stored in (repo / GENERATION_STORE_PATH).iterdir():
        stored.unlink()
    assert _run_policy(repo).policy_digest == before


def test_rule_081_a_policy_edit_does_change_the_digest(repo: Path) -> None:
    before = _run_policy(repo).policy_digest
    workspace = fx.documents()["workspace"]
    workspace["profiles"][0]["tool_policy"]["allow"] = ["repo_read", "submit_report"]
    config = {"schema_version": "1.0", "runtime": workspace}
    (repo / ".ea" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    assert _run_policy(repo).policy_digest != before
