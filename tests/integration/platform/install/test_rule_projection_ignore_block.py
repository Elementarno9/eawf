"""Gate: the managed ignore block covers every generated rule projection.

A repository that authors ``.ea/rules.yaml`` renders ``AGENTS.override.md``
(the policy file), one view per referenced module under
``.ea/rules/views/`` and one role carrier per scoped role under
``.claude/skills/eawf-rules-<role>/`` (see
:mod:`eawf.platform.rules.render`, :mod:`eawf.platform.rules.views` and
:mod:`eawf.platform.rules.carriers`). None of those three is the committed
source: each regenerates from ``.ea/rules.yaml`` on ``eawf sync``, so a
repository whose managed ``.gitignore`` block omits one would commit a
second, drifting copy of generated content -- and for the policy file, the
machine-local workspace layer it composes in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from eawf.platform.install.gitignore_writer import GITIGNORE_PATTERNS, write_gitignore

#: One instance of each generated rule projection, repository-relative.
_GENERATED_PROJECTION_PATHS: tuple[str, ...] = (
    "AGENTS.override.md",
    ".ea/rules/views/eawf.core.vcs.md",
    ".claude/skills/eawf-rules-executor/SKILL.md",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _is_ignored(repo: Path, path: Path) -> bool:
    relative = path.relative_to(repo)
    return _git(repo, "check-ignore", "--quiet", "--no-index", str(relative)).returncode == 0


def _seed_rule_source_repo(target: Path) -> None:
    """Initialise *target* as a git repo authoring ``.ea/rules.yaml``.

    Also writes one instance of each generated projection so the ignore
    check exercises real paths rather than strings nothing ever creates.
    """
    target.mkdir(parents=True, exist_ok=True)
    _git(target, "init", "--quiet")
    (target / ".ea").mkdir(parents=True, exist_ok=True)
    (target / ".ea" / "rules.yaml").write_text("schema_version: 1\nrules: []\n", encoding="utf-8")
    for relative in _GENERATED_PROJECTION_PATHS:
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")


def test_managed_block_ignores_every_generated_rule_projection(tmp_path: Path) -> None:
    """Each rendered projection is git-ignored once the managed block writes."""
    target = tmp_path / "repo"
    _seed_rule_source_repo(target)

    write_gitignore(target)

    for relative in _GENERATED_PROJECTION_PATHS:
        assert _is_ignored(target, target / relative), f"{relative} is not ignored"


def test_managed_block_leaves_the_rule_source_and_card_committed(tmp_path: Path) -> None:
    """The generated-projection patterns stay precise: source and card survive.

    ``.ea/rules.yaml`` is the authored source and ``AGENTS.md`` is the
    committed card; neither is a generated projection, so a pattern that
    over-matched (e.g. a bare ``AGENTS.*`` instead of the literal
    ``AGENTS.override.md``) would be a regression this catches.
    """
    target = tmp_path / "repo"
    _seed_rule_source_repo(target)
    (target / "AGENTS.md").write_text("# committed card\n", encoding="utf-8")

    write_gitignore(target)

    assert not _is_ignored(target, target / ".ea" / "rules.yaml")
    assert not _is_ignored(target, target / "AGENTS.md")


def test_managed_block_reds_when_the_policy_pattern_is_dropped(tmp_path: Path) -> None:
    """Revert-check: dropping ``AGENTS.override.md`` un-ignores the policy file.

    Proves the first gate actually exercises the pattern this wave adds --
    nothing else in the managed block covers the policy file's name.
    """
    target = tmp_path / "repo"
    _seed_rule_source_repo(target)
    reverted = tuple(p for p in GITIGNORE_PATTERNS if p != "AGENTS.override.md")
    assert len(reverted) == len(GITIGNORE_PATTERNS) - 1
    _write_managed_block(target, reverted)

    assert not _is_ignored(target, target / "AGENTS.override.md")
    # The other two generated projections are unaffected by the drop.
    assert _is_ignored(target, target / ".ea/rules/views/eawf.core.vcs.md")
    assert _is_ignored(target, target / ".claude/skills/eawf-rules-executor/SKILL.md")


def test_managed_block_reds_when_the_views_pattern_is_dropped(tmp_path: Path) -> None:
    """Revert-check: dropping ``.ea/rules/views/`` un-ignores a rendered view.

    Nothing else in the managed block covers this directory, so removing
    the pattern is the regression the CR-01 gate must catch.
    """
    target = tmp_path / "repo"
    _seed_rule_source_repo(target)
    reverted = tuple(p for p in GITIGNORE_PATTERNS if p != ".ea/rules/views/")
    assert len(reverted) == len(GITIGNORE_PATTERNS) - 1
    _write_managed_block(target, reverted)

    assert not _is_ignored(target, target / ".ea/rules/views/eawf.core.vcs.md")
    assert _is_ignored(target, target / "AGENTS.override.md")


def _write_managed_block(target: Path, patterns: tuple[str, ...]) -> None:
    """Write a managed block carrying exactly *patterns*, bypassing the writer.

    Used only to simulate an un-patched ``GITIGNORE_PATTERNS`` for the
    revert-checks above.
    """
    lines = ["# BEGIN EAWF:gitignore", *patterns, "# END EAWF:gitignore", ""]
    (target / ".gitignore").write_text("\n".join(lines), encoding="utf-8")
