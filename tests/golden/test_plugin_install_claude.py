"""Golden-tree regression tests for ``eawf plugin install claude``.

For every file under ``tests/golden/plugin_install/claude/``, render
the live plugin tree and assert byte-equality. A failure here means
the renderer drifted — either a template, a registry entry, or a
sibling renderer changed.

To regenerate the fixture (intentional drift):

.. code-block:: bash

    uv run python - <<'PY'
    import shutil
    from pathlib import Path
    from eawf.runtimes.claude.plugin_install import install_plugin

    src = Path("/tmp/eawf-plugin-render")
    if src.exists():
        shutil.rmtree(src)
    src.mkdir(parents=True)
    install_plugin(src, persist_manifest=False)
    golden = Path("tests/golden/plugin_install/claude")
    if golden.exists():
        shutil.rmtree(golden)
    shutil.copytree(src / ".claude", golden)
    PY

Mirrors :file:`tests/golden/test_golden_agents_md.py`'s
byte-equality + dual-stability convention.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eawf.runtimes.claude.plugin_install import install_plugin

_FIXTURE_DIR: Path = Path(__file__).parent / "plugin_install" / "claude"


def _walk_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


@pytest.mark.golden
def test_plugin_install_matches_golden_tree(tmp_path: Path) -> None:
    """Rendered plugin tree is byte-identical to the committed fixture.

    Walks the fixture directory; for every file under
    ``tests/golden/plugin_install/claude/<rel>``, the live render at
    ``<tmp_path>/.claude/<rel>`` must match byte-for-byte.
    """
    install_plugin(tmp_path, persist_manifest=False)
    live_root = tmp_path / ".claude"

    expected_files = _walk_files(_FIXTURE_DIR)
    assert expected_files, "golden fixture is empty — regeneration mistake?"

    for expected_path in expected_files:
        rel = expected_path.relative_to(_FIXTURE_DIR)
        live_path = live_root / rel
        assert live_path.exists(), f"missing live file at {live_path}"
        assert live_path.read_bytes() == expected_path.read_bytes(), (
            f"file {rel} drifted from golden fixture. "
            "If this is intentional, regenerate the fixture (see module docstring)."
        )


@pytest.mark.golden
def test_plugin_install_two_renders_byte_stable(tmp_path: Path) -> None:
    """Two consecutive installs of the same registry produce identical bytes.

    Stronger than the golden check: confirms the renderer itself is
    deterministic (no ``datetime.now()`` sneaks into the output).
    """
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    install_plugin(target_a, persist_manifest=False)
    install_plugin(target_b, persist_manifest=False)

    files_a = _walk_files(target_a / ".claude")
    files_b = _walk_files(target_b / ".claude")
    assert {p.relative_to(target_a / ".claude") for p in files_a} == {
        p.relative_to(target_b / ".claude") for p in files_b
    }
    for path_a in files_a:
        rel = path_a.relative_to(target_a / ".claude")
        path_b = target_b / ".claude" / rel
        assert path_a.read_bytes() == path_b.read_bytes(), f"non-deterministic file: {rel}"


@pytest.mark.golden
def test_plugin_install_skill_md_parses_as_frontmatter(tmp_path: Path) -> None:
    """Acceptance §5: every rendered SKILL.md has the YAML frontmatter shape.

    We do not invoke the actual Claude Code skill loader here; instead
    we pin the on-disk shape (``--- ... ---`` block at the top) that
    the loader contracts on, mirroring the hand-written placeholders.
    """
    install_plugin(tmp_path, persist_manifest=False)
    skills_dir = tmp_path / ".claude" / "skills"
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        assert text.startswith("---\n"), f"{skill_md} missing frontmatter opener"
        # Frontmatter terminates with another ``---`` line.
        rest = text[len("---\n") :]
        assert "\n---\n" in rest, f"{skill_md} missing frontmatter closer"
        # Required keys are present.
        for key in (
            "name:",
            "description:",
            "argument-hint:",
            "user-invocable:",
            "disable-model-invocation:",
        ):
            assert key in text, f"{skill_md} missing {key!r} frontmatter line"


# Regenerate-helper visibility: the fixture path lives next to this test.
def _regenerate_fixture_for_local_inspection(tmp_path: Path) -> None:  # pragma: no cover
    """Helper for human use only — copy the live tree into the golden dir.

    Not invoked by ``pytest`` (no ``test_`` prefix). Kept here so a future
    contributor can see the canonical regeneration recipe inline.
    """
    install_plugin(tmp_path, persist_manifest=False)
    if _FIXTURE_DIR.exists():
        shutil.rmtree(_FIXTURE_DIR)
    shutil.copytree(tmp_path / ".claude", _FIXTURE_DIR)
