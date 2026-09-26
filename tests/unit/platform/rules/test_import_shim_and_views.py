"""Emit the import shim and the module views from the same render transaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pydantic
import pytest
import yaml

from eawf.platform.rules import builtin_rule_modules, render
from eawf.platform.rules.carriers import builtin_carrier_roles, carrier_target
from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    ProjectionManifest,
    RuleProjectionHandEditError,
    RuleProjectionOwnedError,
    classify_projection,
    plan_rule_projections,
    projection_drift,
    read_rule_view,
    render_rule_projections,
)
from eawf.platform.rules.views import (
    VIEW_DIRECTORY,
    ModuleViewRead,
    RuleViewNotFoundError,
    RuleViewStartupImportError,
    refuse_view_imports,
    render_module_views,
    view_target,
)
from eawf.surfaces.render.claude_shim import render_import_shim

_PYTHON = "eawf.craft.python"
CLAUDE_SHIM_TARGET = load_host_facts().claude.import_shim or ""
_TEST = "eawf.craft.test"
_CONSTITUTION = "Keep the release notes in the changelog under the version heading."


def _builtin_carriers() -> tuple[str, ...]:
    """Return the carriers every rule-graph repository renders from builtin rules."""
    return tuple(carrier_target(role) for role in builtin_carrier_roles())


def _write_source(
    root: Path, *, modules: list[str], extra: tuple[dict[str, Any], ...] = ()
) -> None:
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    rules = [
        {
            "rule_id": "repo.changelog",
            "obligation_id": "demo.changelog",
            "revision": 1,
            "title": "Keep release notes in the changelog",
            "zone": "constitution",
            "force": "must",
            "effectiveness": "behavioral",
            "instruction": _CONSTITUTION,
            "verification": {"method": "review"},
        },
        *extra,
    ]
    document = {"schema_version": 1, "modules": modules, "rules": rules}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _write_source(root, modules=[_PYTHON, _TEST])
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


def _view(root: Path, reference: str) -> Path:
    return root / view_target(reference)


# ---- gate-fire proof: stale views and eager view imports --------------------


def test_read_rule_view_reports_a_view_from_an_older_graph_as_stale(repo: Path) -> None:
    render_rule_projections(repo)
    assert read_rule_view(repo, _PYTHON).status == "current"
    extra = {
        "rule_id": "repo.docstrings",
        "obligation_id": "demo.docstrings",
        "revision": 1,
        "title": "Write short module docstrings",
        "zone": "steering",
        "force": "should",
        "effectiveness": "behavioral",
        "instruction": "Prefer short module docstrings that state why the module exists.",
        "verification": {"method": "review"},
    }
    _write_source(repo, modules=[_PYTHON, _TEST], extra=(extra,))
    read = read_rule_view(repo, _PYTHON)
    assert read.status == "stale"
    assert read.message is not None
    assert "not the current" in read.message
    assert "eawf sync" in read.message
    render_rule_projections(repo)
    assert read_rule_view(repo, _PYTHON).status == "current"


def test_render_import_shim_refuses_an_eager_import_of_every_view(repo: Path) -> None:
    views = [view_target(reference) for reference in (_PYTHON, _TEST)]
    with pytest.raises(RuleViewStartupImportError, match="startup import may not load"):
        render_import_shim((POLICY_TARGET, *views))


def test_refuse_view_imports_accepts_projections_and_refuses_one_view() -> None:
    refuse_view_imports((POLICY_TARGET, CARD_TARGET))
    refuse_view_imports(())
    with pytest.raises(RuleViewStartupImportError) as caught:
        refuse_view_imports((f"{VIEW_DIRECTORY}/{_TEST}.md",))
    assert caught.value.code == "rule_view_startup_import"


# ---- the import shim --------------------------------------------------------


def test_render_rule_projections_writes_the_shim_importing_the_policy(repo: Path) -> None:
    written = render_rule_projections(repo)
    shim = (repo / CLAUDE_SHIM_TARGET).read_text(encoding="utf-8")
    assert shim == f"@{POLICY_TARGET}\n"
    assert CLAUDE_SHIM_TARGET in written.changed
    assert CLAUDE_SHIM_TARGET in written.manifest.generated
    policy = (repo / POLICY_TARGET).read_text(encoding="utf-8")
    python = builtin_rule_modules()[_PYTHON]
    must = [record for record in python.records if record.force == "must"]
    assert must
    for record in must:
        assert record.instruction in policy


def test_render_rule_projections_replaces_the_legacy_card_shim(repo: Path) -> None:
    (repo / CLAUDE_SHIM_TARGET).write_text("@AGENTS.md\n", encoding="utf-8")
    assert classify_projection(repo / CLAUDE_SHIM_TARGET) == "generated"
    render_rule_projections(repo)
    assert (repo / CLAUDE_SHIM_TARGET).read_text(encoding="utf-8") == f"@{POLICY_TARGET}\n"


def test_render_rule_projections_refuses_a_shim_holding_prose(repo: Path) -> None:
    shim = repo / CLAUDE_SHIM_TARGET
    shim.write_text("@AGENTS.md\nAlways push to main.\n", encoding="utf-8")
    with pytest.raises(RuleProjectionOwnedError):
        render_rule_projections(repo)
    assert shim.read_text(encoding="utf-8") == "@AGENTS.md\nAlways push to main.\n"
    assert not (repo / CARD_TARGET).exists()


def test_render_import_shim_single_import() -> None:
    assert render_import_shim((POLICY_TARGET,)) == "@AGENTS.override.md\n"


def test_render_import_shim_keeps_import_order() -> None:
    assert render_import_shim(("b.md", "a.md")).splitlines() == ["@b.md", "@a.md"]


@pytest.mark.parametrize(
    "imports", [(), ("",), ("/abs/AGENTS.md",), ("../AGENTS.md",), ("a b.md",)]
)
def test_render_import_shim_refuses_invalid_imports(imports: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="import"):
        render_import_shim(imports)


def test_render_import_shim_view_refusal_is_a_value_error() -> None:
    with pytest.raises(ValueError, match="views are read on demand"):
        render_import_shim((view_target(_PYTHON),))


# ---- the module views -------------------------------------------------------


def test_render_rule_projections_writes_one_stamped_view_per_selected_module(repo: Path) -> None:
    written = render_rule_projections(repo)
    plan = plan_rule_projections(repo)
    policy_digest = plan.manifest.projections[1].graph_digest
    for reference in (_PYTHON, _TEST):
        text = _view(repo, reference).read_text(encoding="utf-8")
        assert text.startswith(f"<!-- eawf:projection kind=view graph={policy_digest} ")
        assert f"module={reference} " in text
        assert view_target(reference) in written.changed
        for record in builtin_rule_modules()[reference].records:
            assert record.instruction in text
            assert f"`{record.rule_id}`" in text
    assert written.manifest.generated == (
        CLAUDE_SHIM_TARGET,
        view_target(_PYTHON),
        view_target(_TEST),
        *_builtin_carriers(),
    )


def test_render_rule_projections_views_are_never_in_the_card(repo: Path) -> None:
    render_rule_projections(repo)
    card = (repo / CARD_TARGET).read_text(encoding="utf-8")
    assert VIEW_DIRECTORY not in card
    assert f"read via: `eawf rules view {_TEST}`" in card


def test_render_rule_projections_second_render_changes_no_view(repo: Path) -> None:
    render_rule_projections(repo)
    again = render_rule_projections(repo)
    assert again.changed == ()
    assert projection_drift(repo, plan_rule_projections(repo)) == ()


def test_projection_drift_names_a_missing_view_and_shim(repo: Path) -> None:
    render_rule_projections(repo)
    _view(repo, _TEST).unlink()
    (repo / CLAUDE_SHIM_TARGET).unlink()
    assert projection_drift(repo, plan_rule_projections(repo)) == (
        CLAUDE_SHIM_TARGET,
        view_target(_TEST),
    )


def test_render_rule_projections_removes_the_view_of_a_deselected_module(repo: Path) -> None:
    render_rule_projections(repo)
    _write_source(repo, modules=[_PYTHON])
    written = render_rule_projections(repo)
    assert written.removed == (view_target(_TEST),)
    assert not _view(repo, _TEST).exists()
    assert _view(repo, _PYTHON).is_file()
    manifest = ProjectionManifest.model_validate_json(
        (repo / PROJECTION_MANIFEST_PATH).read_bytes()
    )
    assert view_target(_TEST) not in manifest.targets


def test_render_rule_projections_refuses_a_hand_edited_view(repo: Path) -> None:
    render_rule_projections(repo)
    view = _view(repo, _TEST)
    view.write_text(view.read_text(encoding="utf-8") + "- a hand-added rule\n", encoding="utf-8")
    assert classify_projection(view) == "hand_edited"
    with pytest.raises(RuleProjectionHandEditError):
        render_rule_projections(repo)


def test_render_rule_projections_interrupted_view_write_restores_every_target(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = render._atomic_write_bytes

    def interrupted(path: Path, payload: bytes) -> None:
        if path.parent.name == "views":
            raise OSError("disk full")
        real_write(path, payload)

    monkeypatch.setattr(render, "_atomic_write_bytes", interrupted)
    with pytest.raises(OSError, match="disk full"):
        render_rule_projections(repo)
    for target in (CARD_TARGET, POLICY_TARGET, CLAUDE_SHIM_TARGET, PROJECTION_MANIFEST_PATH):
        assert not (repo / target).exists()


def test_render_module_views_without_selection_is_empty(repo: Path) -> None:
    _write_source(repo, modules=[])
    plan = plan_rule_projections(repo)
    assert [g.target for g in plan.generated] == [CLAUDE_SHIM_TARGET, *_builtin_carriers()]


def test_render_module_views_module_with_no_rule_in_effect(repo: Path) -> None:
    selection, _graph = render._policy_graph(repo, home=None)
    _write_source(repo, modules=[])
    _selection, bare = render._policy_graph(repo, home=None)
    catalog_only = selection.model_copy(
        update={"entries": tuple(e for e in selection.entries if e.reference == _TEST)}
    )
    (view,) = render_module_views(catalog_only, bare)
    assert "No rule of this module is in effect" in view.text


def test_render_module_views_skips_an_unavailable_reference(repo: Path) -> None:
    _write_source(repo, modules=[_PYTHON, "eawf.craft.nonexistent"])
    plan = plan_rule_projections(repo)
    assert [g.target for g in plan.generated] == [
        CLAUDE_SHIM_TARGET,
        view_target(_PYTHON),
        *_builtin_carriers(),
    ]


# ---- reading a view ---------------------------------------------------------


def test_read_rule_view_absent_names_the_generating_command(repo: Path) -> None:
    read = read_rule_view(repo, _PYTHON)
    assert read.status == "absent"
    assert read.text is None
    assert read.message is not None
    assert "run eawf sync" in read.message


def test_read_rule_view_current_returns_the_body_without_the_stamp(repo: Path) -> None:
    render_rule_projections(repo)
    read = read_rule_view(repo, _TEST)
    assert read.status == "current"
    assert read.message is None
    assert read.text is not None
    assert read.text.startswith("# ")


def test_read_rule_view_hand_edited_view_is_stale(repo: Path) -> None:
    render_rule_projections(repo)
    view = _view(repo, _TEST)
    view.write_text(view.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    read = read_rule_view(repo, _TEST)
    assert read.status == "stale"
    assert read.message is not None
    assert "edited since it was rendered" in read.message


def test_read_rule_view_unstamped_view_is_stale(repo: Path) -> None:
    view = _view(repo, _TEST)
    view.parent.mkdir(parents=True)
    view.write_text("# old notes\n", encoding="utf-8")
    read = read_rule_view(repo, _TEST)
    assert read.status == "stale"
    assert read.text == "# old notes\n"


def test_read_rule_view_refuses_an_unselected_module(repo: Path) -> None:
    with pytest.raises(RuleViewNotFoundError, match="not selected"):
        read_rule_view(repo, "eawf.craft.markdown")


def test_read_rule_view_refuses_an_unavailable_module(repo: Path) -> None:
    _write_source(repo, modules=["eawf.craft.nonexistent"])
    with pytest.raises(RuleViewNotFoundError, match="unavailable"):
        read_rule_view(repo, "eawf.craft.nonexistent")


def test_module_view_read_refuses_inconsistent_payload() -> None:
    with pytest.raises(pydantic.ValidationError):
        ModuleViewRead(
            reference=_TEST, target=view_target(_TEST), status="absent", text="x", message="m"
        )
    with pytest.raises(pydantic.ValidationError):
        ModuleViewRead(
            reference=_TEST, target=view_target(_TEST), status="stale", text="x", message=None
        )
