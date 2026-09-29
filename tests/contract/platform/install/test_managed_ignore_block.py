"""The managed ignore block is the one committed declaration of what is generated.

The render transaction regenerates the delimited block in ``.gitignore``,
keeps every byte outside it, overwrites hand edits inside it, and refuses
before any write a generated path the block does not ignore.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from eawf.platform.install import gitignore_writer
from eawf.platform.install.gitignore_writer import (
    GITIGNORE_PATTERNS,
    plan_gitignore_block,
    unenumerated_paths,
)
from eawf.platform.rules.render import (
    CARD_TARGET,
    PROJECTION_MANIFEST_PATH,
    RuleProjectionUnenumeratedError,
    plan_rule_projections,
    projection_drift,
    render_rule_projections,
)

_BEGIN = "# BEGIN EAWF:gitignore"
_END = "# END EAWF:gitignore"
_OPERATOR = "# operator header\nnode_modules/\n\n"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = tmp_path / "demo"
    source = root / ".ea" / "rules.yaml"
    source.parent.mkdir(parents=True)
    document = {"schema_version": 1, "modules": ["eawf.craft.python"], "rules": []}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    return root


def _ignored_by_git(root: Path, relative: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", relative], cwd=root, check=False
    )
    return result.returncode == 0


def _generated(root: Path) -> list[str]:
    manifest = plan_rule_projections(root).manifest
    return [
        *(record.target for record in manifest.projections if record.kind != "card"),
        *manifest.generated,
        PROJECTION_MANIFEST_PATH,
    ]


def test_surf_174_fresh_write_ignores_every_generated_path(repo: Path) -> None:
    written = render_rule_projections(repo)
    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(f"{_BEGIN}\n") and text.endswith(f"{_END}\n")
    assert ".gitignore" in written.changed
    assert written.gitignore_patterns_added == tuple(dict.fromkeys(GITIGNORE_PATTERNS))
    generated = _generated(repo)
    assert generated
    for relative in generated:
        assert _ignored_by_git(repo, relative), relative
    assert not _ignored_by_git(repo, CARD_TARGET)
    assert not _ignored_by_git(repo, ".ea/rules.yaml")


def test_surf_174_idempotent_rewrite_changes_no_byte(repo: Path) -> None:
    (repo / ".gitignore").write_text(_OPERATOR, encoding="utf-8")
    render_rule_projections(repo)
    first = (repo / ".gitignore").read_bytes()
    written = render_rule_projections(repo)
    assert (repo / ".gitignore").read_bytes() == first
    assert ".gitignore" not in written.changed
    assert written.gitignore_patterns_added == ()
    assert ".gitignore" not in projection_drift(repo, plan_rule_projections(repo))


def test_surf_174_hand_edit_inside_the_block_is_restored(repo: Path) -> None:
    (repo / ".gitignore").write_text(_OPERATOR, encoding="utf-8")
    render_rule_projections(repo)
    rendered = (repo / ".gitignore").read_text(encoding="utf-8")
    edited = rendered.replace("AGENTS.override.md\n", "junk/\n").replace(
        f"{_END}\n", f"/custom/state.lock\n{_END}\n"
    )
    (repo / ".gitignore").write_text(edited + "tail/\n", encoding="utf-8")
    assert ".gitignore" in projection_drift(repo, plan_rule_projections(repo))

    written = render_rule_projections(repo)

    text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert text.startswith(_OPERATOR)
    assert text.endswith(f"/custom/state.lock\n{_END}\ntail/\n")
    assert "AGENTS.override.md\n" in text
    assert "junk/" not in text
    assert written.gitignore_patterns_added == ("AGENTS.override.md",)


def test_surf_174_unenumerated_generated_path_is_refused_before_any_write(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / ".gitignore").write_text(_OPERATOR, encoding="utf-8")
    shipped = tuple(p for p in GITIGNORE_PATTERNS if p != ".ea/rules/views/")
    monkeypatch.setattr(gitignore_writer, "GITIGNORE_PATTERNS", shipped)
    with pytest.raises(RuleProjectionUnenumeratedError, match=r"\.ea/rules/views/"):
        render_rule_projections(repo)
    assert (repo / ".gitignore").read_text(encoding="utf-8") == _OPERATOR
    assert sorted(p.name for p in repo.iterdir()) == [".ea", ".git", ".gitignore", "pyproject.toml"]
    assert not (repo / PROJECTION_MANIFEST_PATH).exists()


def test_surf_174_plan_without_a_gitignore_is_the_block_alone(tmp_path: Path) -> None:
    plan = plan_gitignore_block(tmp_path)
    assert plan.payload.decode("utf-8").splitlines()[1:-1] == list(plan.patterns)
    assert plan.path == (tmp_path / ".gitignore").resolve()
    assert not plan.path.exists()


# ---- the coverage matcher ------------------------------------------------------


def test_surf_174_unenumerated_paths_empty_inputs() -> None:
    assert unenumerated_paths([], []) == ()
    assert unenumerated_paths(["CLAUDE.md"], []) == ()
    assert unenumerated_paths([], ["CLAUDE.md"]) == ("CLAUDE.md",)


@pytest.mark.parametrize(
    ("pattern", "path", "ignored"),
    [
        ("CLAUDE.md", "CLAUDE.md", True),
        ("CLAUDE.md", "sub/CLAUDE.md", True),
        ("CLAUDE.md", "CLAUDE.mdx", False),
        (".claude/", ".claude/skills/x/SKILL.md", True),
        (".claude/", ".claude", False),
        (".ea/rules/views/", ".ea/rules/views/eawf.core.vcs.md", True),
        (".ea/rules/views/", "nested/.ea/rules/views/x.md", False),
        (".claude/skills/eawf-rules-*/", ".claude/skills/eawf-rules-executor/SKILL.md", True),
        (".claude/skills/eawf-rules-*/", ".claude/skills/mine/SKILL.md", False),
        ("*.db", ".ea/telemetry.db", True),
        (".ea/**/*.lock", ".ea/store/deep/x.lock", True),
        ("/AGENTS.override.md", "AGENTS.override.md", True),
        ("/AGENTS.override.md", "docs/AGENTS.override.md", False),
        ("# AGENTS.override.md", "AGENTS.override.md", False),
        ("!AGENTS.override.md", "AGENTS.override.md", False),
        ("   ", "AGENTS.override.md", False),
    ],
)
def test_surf_174_unenumerated_paths_follows_gitignore_matching(
    pattern: str, path: str, ignored: bool
) -> None:
    assert unenumerated_paths([pattern], [path]) == (() if ignored else (path,))


def test_surf_174_unenumerated_paths_keeps_input_order() -> None:
    assert unenumerated_paths(["a.md"], ["z.md", "a.md", "b.md"]) == ("z.md", "b.md")
