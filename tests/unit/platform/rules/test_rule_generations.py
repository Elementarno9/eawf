"""Render transaction generations: obsolete removal, rollback and shadow validation.

RULE-072: a fresh render equals enable-then-render-then-disable-then-render.
RULE-073: a failed budget or validation leaves the selected output and
manifest unchanged, and a rollback selects a complete stored generation.
RULE-093: a shadow generation is validated before selection, and selection
is atomic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.platform.rules import render
from eawf.platform.rules.generations import (
    GENERATION_STORE_PATH,
    RETAINED_GENERATIONS,
    RuleGenerationError,
    StoredGeneration,
    generation_path,
    rollback_rule_projections,
    stored_generations,
    verify_generation,
)
from eawf.platform.rules.host_facts import HostFactRegistry, load_host_facts
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    GeneratedFile,
    ProjectionPlan,
    RuleProjectionBudgetError,
    RuleProjectionHandEditError,
    RuleProjectionShadowError,
    plan_rule_projections,
    render_rule_projections,
)
from eawf.platform.rules.views import view_target

pytestmark = pytest.mark.usefixtures("isolated_host_homes")

_MARKDOWN = "eawf.craft.markdown"


def _rule(rule_id: str, instruction: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "rule_id": f"repo.{rule_id}",
        "obligation_id": f"demo.{rule_id}",
        "revision": 1,
        "title": f"Demo rule {rule_id}",
        "zone": "steering",
        "force": "should",
        "effectiveness": "behavioral",
        "instruction": instruction,
        "verification": {"method": "review"},
    }
    body.update(overrides)
    return body


_BASE_RULES = [_rule("docstrings", "Prefer short module docstrings that state why.")]


def _write_source(
    root: Path,
    *,
    modules: list[str] | None = None,
    rules: list[dict[str, Any]] | None = None,
    legacy: list[dict[str, Any]] | None = None,
) -> None:
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "schema_version": 1,
        "modules": ["eawf.craft.python"] if modules is None else modules,
        "rules": _BASE_RULES if rules is None else rules,
    }
    if legacy is not None:
        document["legacy"] = legacy
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _make_repo(root: Path) -> Path:
    _write_source(root)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nrequires-python = ">=3.14"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_repo(tmp_path / "demo")


def _tree(root: Path) -> dict[str, bytes]:
    """Every rendered file under ``root``, excluding the generation history."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and not path.relative_to(root).as_posix().startswith(GENERATION_STORE_PATH)
    }


def _selected(root: Path) -> dict[str, bytes]:
    return {k: v for k, v in _tree(root).items() if not k.startswith(".ea/rules.yaml")}


def _codex_cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    base = load_host_facts()
    fact = base.codex.project_document_cap_bytes.model_copy(update={"default_value": cap})
    codex = base.codex.model_copy(update={"project_document_cap_bytes": fact})
    facts: HostFactRegistry = base.model_copy(update={"codex": codex})
    monkeypatch.setattr(render, "load_host_facts", lambda: facts)


# ---- RULE-072: obsolete targets and fresh-equals-incremental ---------------


def test_rule_072_enable_render_disable_render_equals_a_fresh_render(
    repo: Path, tmp_path: Path
) -> None:
    render_rule_projections(repo)
    extra = _rule("extra", "Keep one module per concern.")
    _write_source(repo, modules=["eawf.craft.python", _MARKDOWN], rules=[*_BASE_RULES, extra])
    render_rule_projections(repo)
    _write_source(repo)
    render_rule_projections(repo)
    fresh = _make_repo(tmp_path / "fresh")
    render_rule_projections(fresh)
    assert _tree(repo) == _tree(fresh)


def test_rule_072_disabling_a_module_removes_its_generated_view(repo: Path) -> None:
    _write_source(repo, modules=["eawf.craft.python", _MARKDOWN])
    render_rule_projections(repo)
    assert (repo / view_target(_MARKDOWN)).is_file()
    _write_source(repo)
    written = render_rule_projections(repo)
    assert written.removed == (view_target(_MARKDOWN),)
    assert not (repo / view_target(_MARKDOWN)).exists()
    assert view_target(_MARKDOWN) not in written.manifest.targets


# ---- RULE-073: failures leave the selection alone; rollback is whole -------


def test_rule_073_failed_budget_leaves_output_and_manifest_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_rule_projections(repo)
    before = _tree(repo)
    history = stored_generations(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    _codex_cap(monkeypatch, 64)
    with pytest.raises(RuleProjectionBudgetError):
        render_rule_projections(repo)
    assert _selected(repo) == {k: v for k, v in before.items() if k != ".ea/rules.yaml"}
    assert stored_generations(repo) == history


def test_rule_073_failed_validation_leaves_output_and_manifest_unchanged(repo: Path) -> None:
    render_rule_projections(repo)
    before = _selected(repo)
    legacy = [{"item": "block:old", "disposition": "duplicate_of", "obligation_id": "demo.none"}]
    _write_source(repo, legacy=legacy)
    with pytest.raises(RuleProjectionShadowError, match="owned by no rule"):
        render_rule_projections(repo)
    assert _selected(repo) == before


def test_rule_073_rollback_selects_the_previous_generation_byte_for_byte(repo: Path) -> None:
    first = render_rule_projections(repo)
    before = _selected(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    second = render_rule_projections(repo)
    assert second.manifest.generation != first.manifest.generation
    # A source that no longer compiles proves the rollback never re-renders.
    (repo / ".ea" / "rules.yaml").write_text("schema_version: 99\n", encoding="utf-8")
    rolled = rollback_rule_projections(repo, None)
    assert rolled.manifest == first.manifest
    assert _selected(repo) == before


def test_rule_073_rollback_names_a_generation_by_digest(repo: Path) -> None:
    first = render_rule_projections(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    second = render_rule_projections(repo)
    rollback_rule_projections(repo, first.manifest.generation)
    again = rollback_rule_projections(repo, second.manifest.generation)
    assert again.manifest == second.manifest


def test_rule_073_rollback_without_a_previous_generation_is_refused(repo: Path) -> None:
    render_rule_projections(repo)
    with pytest.raises(RuleGenerationError, match="before the current one"):
        rollback_rule_projections(repo, None)


def test_rule_073_rollback_to_an_unknown_generation_is_refused(repo: Path) -> None:
    render_rule_projections(repo)
    with pytest.raises(RuleGenerationError, match="sha256:"):
        rollback_rule_projections(repo, f"sha256:{'0' * 64}")


def test_rule_073_rollback_on_an_empty_store_is_refused(repo: Path) -> None:
    with pytest.raises(RuleGenerationError):
        rollback_rule_projections(repo, None)


def test_rule_073_rollback_refuses_a_hand_edited_target(repo: Path) -> None:
    render_rule_projections(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    render_rule_projections(repo)
    policy = repo / POLICY_TARGET
    policy.write_text(policy.read_text(encoding="utf-8") + "- hand edit\n", encoding="utf-8")
    before = _selected(repo)
    with pytest.raises(RuleProjectionHandEditError):
        rollback_rule_projections(repo, None)
    assert _selected(repo) == before


def test_rule_073_store_keeps_exactly_the_retained_generations(repo: Path) -> None:
    for index in range(RETAINED_GENERATIONS + 2):
        _write_source(repo, rules=[*_BASE_RULES, _rule("extra", f"Name owner {index} of it.")])
        render_rule_projections(repo)
    stored = stored_generations(repo)
    assert len(stored) == RETAINED_GENERATIONS
    assert len(list((repo / GENERATION_STORE_PATH).glob("*.json"))) == RETAINED_GENERATIONS
    current = json.loads((repo / PROJECTION_MANIFEST_PATH).read_text("utf-8"))["generation"]
    assert stored[0].plan.manifest.generation == current


def test_rule_073_unreadable_stored_generation_is_skipped(repo: Path) -> None:
    render_rule_projections(repo)
    (repo / GENERATION_STORE_PATH / "broken.json").write_text("{", encoding="utf-8")
    assert len(stored_generations(repo)) == 1


# ---- RULE-093: shadow validation before an atomic selection ----------------


def _plan(repo: Path) -> ProjectionPlan:
    return plan_rule_projections(repo)


def test_rule_093_a_fresh_generation_verifies(repo: Path) -> None:
    verify_generation(_plan(repo))


def test_rule_093_trust_refuses_output_that_differs_from_its_manifest(repo: Path) -> None:
    plan = _plan(repo)
    card = plan.projections[0].model_copy(update={"text": plan.projections[0].text + "x"})
    tampered = plan.model_copy(update={"projections": (card, *plan.projections[1:])})
    with pytest.raises(RuleProjectionShadowError, match="does not match its manifest row"):
        verify_generation(tampered)


def test_rule_093_trust_refuses_a_generation_digest_that_does_not_recompute(repo: Path) -> None:
    plan = _plan(repo)
    manifest = plan.manifest.model_copy(update={"generation": f"sha256:{'1' * 64}"})
    with pytest.raises(RuleProjectionShadowError, match="does not recompute"):
        verify_generation(plan.model_copy(update={"manifest": manifest}))


def test_rule_093_budget_refuses_a_projection_over_its_recorded_cap(repo: Path) -> None:
    plan = _plan(repo)
    record = plan.projections[0].record
    over = record.model_copy(update={"cap_bytes": record.byte_count - 1})
    projection = plan.projections[0].model_copy(update={"record": over})
    manifest = plan.manifest.model_copy(
        update={"projections": (over, *plan.manifest.projections[1:])}
    )
    tampered = plan.model_copy(
        update={"projections": (projection, *plan.projections[1:]), "manifest": manifest}
    )
    with pytest.raises(RuleProjectionShadowError, match="over its"):
        verify_generation(tampered)


def test_rule_093_host_loading_refuses_a_shim_importing_an_unrendered_file(repo: Path) -> None:
    plan = _plan(repo)
    shim = next(g for g in plan.generated if g.text.startswith("@"))
    broken = GeneratedFile(target=shim.target, text="@MISSING.md\n")
    tampered = plan.model_copy(
        update={"generated": tuple(broken if g is shim else g for g in plan.generated)}
    )
    with pytest.raises(RuleProjectionShadowError, match=r"imports MISSING\.md"):
        verify_generation(tampered)


def test_rule_093_commands_refuse_a_view_command_without_its_view(repo: Path) -> None:
    plan = _plan(repo)
    kept = tuple(g for g in plan.generated if g.target != view_target("eawf.craft.python"))
    manifest = plan.manifest.model_copy(update={"generated": tuple(g.target for g in kept)})
    tampered = plan.model_copy(update={"generated": kept, "manifest": manifest})
    with pytest.raises(RuleProjectionShadowError, match="renders no such view"):
        verify_generation(tampered)


def test_rule_093_paths_refuse_a_repository_procedure_that_does_not_exist(repo: Path) -> None:
    procedure = _rule("proc", "Follow the release procedure.", procedure_ref="docs/release.md")
    _write_source(repo, rules=[*_BASE_RULES, procedure])
    with pytest.raises(RuleProjectionShadowError, match=r"docs/release\.md"):
        plan_rule_projections(repo)
    (repo / "docs").mkdir()
    (repo / "docs" / "release.md").write_text("# Release\n", encoding="utf-8")
    plan_rule_projections(repo)


def test_rule_093_coverage_refuses_a_disposition_whose_obligation_is_unowned(repo: Path) -> None:
    owned = [{"item": "block:a", "disposition": "duplicate_of", "obligation_id": "demo.docstrings"}]
    _write_source(repo, legacy=owned)
    plan_rule_projections(repo)
    unowned = [{"item": "block:a", "disposition": "steering_rule", "obligation_id": "demo.gone"}]
    _write_source(repo, legacy=unowned)
    with pytest.raises(RuleProjectionShadowError, match=r"block:a -> demo\.gone"):
        plan_rule_projections(repo)


def test_rule_093_a_stored_generation_is_verified_again_before_rollback(repo: Path) -> None:
    first = render_rule_projections(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    render_rule_projections(repo)
    before = _selected(repo)
    path = generation_path(repo, first.manifest.generation)
    stored = StoredGeneration.model_validate_json(path.read_bytes())
    card = stored.plan.projections[0]
    tampered = stored.plan.model_copy(
        update={
            "projections": (
                card.model_copy(update={"text": card.text + "- injected\n"}),
                *stored.plan.projections[1:],
            )
        }
    )
    path.write_text(stored.model_copy(update={"plan": tampered}).model_dump_json(), "utf-8")
    with pytest.raises(RuleProjectionShadowError):
        rollback_rule_projections(repo, first.manifest.generation)
    assert _selected(repo) == before


def test_rule_093_selection_writes_the_manifest_last(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = render._atomic_write_bytes
    calls: list[Path] = []

    def recording(path: Path, payload: bytes) -> None:
        calls.append(path)
        real_write(path, payload)

    monkeypatch.setattr(render, "_atomic_write_bytes", recording)
    render_rule_projections(repo)
    assert calls[0] == repo / CARD_TARGET
    assert calls[-1] == repo / PROJECTION_MANIFEST_PATH


def test_rule_093_interrupted_selection_restores_every_target_and_the_store(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_rule_projections(repo)
    before = _selected(repo)
    history = stored_generations(repo)
    _write_source(repo, rules=[*_BASE_RULES, _rule("extra", "Name the owner of each module.")])
    real_write = render._atomic_write_bytes

    def interrupted(path: Path, payload: bytes) -> None:
        if path == repo / PROJECTION_MANIFEST_PATH:
            raise OSError("power cut")
        real_write(path, payload)

    monkeypatch.setattr(render, "_atomic_write_bytes", interrupted)
    with pytest.raises(OSError, match="power cut"):
        render_rule_projections(repo)
    assert _selected(repo) == before
    assert stored_generations(repo) == history
