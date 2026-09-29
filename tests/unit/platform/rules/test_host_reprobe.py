"""Removal is confirmed against what each host loads, not against the manifest."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.platform.rules.carriers import carrier_target
from eawf.platform.rules.host_facts import load_host_facts
from eawf.platform.rules.host_probe import host_loaded_files
from eawf.platform.rules.render import (
    CARD_TARGET,
    POLICY_TARGET,
    PROJECTION_MANIFEST_PATH,
    RuleProjectionHandEditError,
    RuleProjectionRemovalError,
    render_rule_projections,
)

_PROJECTIONS: dict[str, str] = {"card": CARD_TARGET, "policy": POLICY_TARGET}
_CONSTITUTION: dict[str, Any] = {
    "rule_id": "repo.changelog",
    "obligation_id": "demo.changelog",
    "revision": 1,
    "title": "Keep the changelog",
    "zone": "constitution",
    "force": "must",
    "effectiveness": "behavioral",
    "instruction": "Keep the release notes in the changelog under the version heading.",
    "verification": {"method": "review"},
}
_REVIEWER_RULE: dict[str, Any] = {
    "rule_id": "repo.review-scope",
    "obligation_id": "demo.review-scope",
    "revision": 1,
    "title": "Review the declared scope",
    "zone": "retrievable",
    "force": "should",
    "effectiveness": "behavioral",
    "instruction": "Review only the paths the change declares.",
    "scope": {"roles": ["reviewer"]},
    "verification": {"method": "review"},
}
_REVIEWER_CARRIER = carrier_target("reviewer")


def _write_source(root: Path, *rules: dict[str, Any]) -> None:
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True, exist_ok=True)
    document = {"schema_version": 1, "modules": [], "rules": [_CONSTITUTION, *rules]}
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    _write_source(root, _REVIEWER_RULE)
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    return root


def _loaded(root: Path) -> tuple[str, ...]:
    return host_loaded_files(root, load_host_facts(), projections=_PROJECTIONS)


# ---- the disable-then-render cycle ------------------------------------------


def test_surf_009_disabled_role_carrier_leaves_the_host_chain_with_the_manifest_lost(
    repo: Path,
) -> None:
    render_rule_projections(repo)
    assert _REVIEWER_CARRIER in _loaded(repo)
    _write_source(repo)
    (repo / PROJECTION_MANIFEST_PATH).unlink()

    written = render_rule_projections(repo)

    assert _REVIEWER_CARRIER in written.removed
    assert _REVIEWER_CARRIER not in _loaded(repo)
    assert not (repo / _REVIEWER_CARRIER).exists()


def test_surf_009_disabled_role_carrier_leaves_the_host_chain_with_the_manifest_kept(
    repo: Path,
) -> None:
    render_rule_projections(repo)
    _write_source(repo)
    written = render_rule_projections(repo)
    assert written.removed == (_REVIEWER_CARRIER,)
    assert _REVIEWER_CARRIER not in _loaded(repo)


def test_surf_009_removal_that_did_not_take_effect_fails_and_restores(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render_rule_projections(repo)
    carrier = repo / _REVIEWER_CARRIER
    before = carrier.read_bytes()
    policy_before = (repo / POLICY_TARGET).read_bytes()
    _write_source(repo)
    real_unlink = Path.unlink

    def unlink_all_but_the_carrier(self: Path, missing_ok: bool = False) -> None:
        if self != carrier:
            real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink_all_but_the_carrier)
    with pytest.raises(RuleProjectionRemovalError, match=_REVIEWER_CARRIER):
        render_rule_projections(repo)
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert carrier.read_bytes() == before
    assert (repo / POLICY_TARGET).read_bytes() == policy_before


def test_surf_009_hand_edited_stale_carrier_is_refused_and_kept(repo: Path) -> None:
    render_rule_projections(repo)
    _write_source(repo)
    (repo / PROJECTION_MANIFEST_PATH).unlink()
    carrier = repo / _REVIEWER_CARRIER
    carrier.write_text(carrier.read_text(encoding="utf-8") + "edit\n", encoding="utf-8")
    with pytest.raises(RuleProjectionHandEditError):
        render_rule_projections(repo)
    assert carrier.exists()


def test_surf_009_operator_skill_and_import_only_file_are_never_removed(repo: Path) -> None:
    render_rule_projections(repo)
    skill = repo / ".claude" / "skills" / "mine" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: mine\n---\nMy own skill.\n", encoding="utf-8")
    shim_like = repo / ".claude" / "skills" / "imports" / "SKILL.md"
    shim_like.parent.mkdir(parents=True)
    shim_like.write_text("@AGENTS.md\n", encoding="utf-8")
    written = render_rule_projections(repo)
    assert written.removed == ()
    assert skill.exists()
    assert shim_like.exists()


# ---- the loading probe --------------------------------------------------------


def test_surf_009_host_loaded_files_empty_repository_loads_nothing(tmp_path: Path) -> None:
    assert _loaded(tmp_path) == ()


def test_surf_009_host_loaded_files_follows_the_shim_and_the_reads(repo: Path) -> None:
    render_rule_projections(repo)
    loaded = _loaded(repo)
    assert {"CLAUDE.md", POLICY_TARGET, CARD_TARGET} <= set(loaded)
    assert ".ea/rules/views/eawf.core.vcs.md" not in loaded


def test_surf_009_host_loaded_files_follows_nested_imports_once(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("@docs/a.md\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("@b.md\n@./a.md\n", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("@a.md\nplain\n", encoding="utf-8")
    assert _loaded(tmp_path) == ("CLAUDE.md", "docs/a.md", "docs/b.md")


def test_surf_009_host_loaded_files_never_leaves_the_repository(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "outside.md").write_text("secret\n", encoding="utf-8")
    imports = ["@../outside.md", "@absent.md", "@"]
    (root / "CLAUDE.md").write_text("".join(f"{line}\n" for line in imports), encoding="utf-8")
    assert _loaded(root) == ("CLAUDE.md",)
