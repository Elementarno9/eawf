"""Golden-tree regression tests for ``eawf plugin install opencode``.

The fixture pins the OpenCode project-scope render, excluding the sidecar
hash file covered by unit tests. Regenerate only when an intentional
renderer change lands:

.. code-block:: bash

    uv run python - <<'PY'
    import shutil
    import tempfile
    from pathlib import Path
    from eawf.runtime.runtimes.opencode.plugin_install import install_plugin

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp)
        install_plugin(src, persist_manifest=False)
        golden = Path("tests/golden/plugin_install/opencode")
        if golden.exists():
            shutil.rmtree(golden)
        for path in sorted(p for p in src.rglob("*") if p.is_file()):
            if path.name == ".eawf-managed.json":
                continue
            rel = path.relative_to(src)
            if rel.parts[0] == ".opencode":
                rel = Path(*rel.parts[1:])
            fixture_path = golden / rel.with_name(f"{rel.name}.golden")
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, fixture_path)
    PY
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.runtimes.opencode.plugin_install import install_plugin

_FIXTURE_DIR: Path = Path(__file__).parent / "plugin_install" / "opencode"
_SIDECAR_FILENAME: str = ".eawf-managed.json"


def _walk_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _fixture_rel_for_live_path(path: Path, root: Path) -> Path | None:
    rel = path.relative_to(root)
    if rel.name == _SIDECAR_FILENAME:
        return None
    if rel.parts[0] == ".opencode":
        rel = Path(*rel.parts[1:])
    return rel.with_name(f"{rel.name}.golden")


def _live_path_for_fixture_rel(rel: Path, root: Path) -> Path:
    live_rel = rel.with_name(rel.name.removesuffix(".golden"))
    if live_rel.parts[0] == "opencode.json":
        return root / live_rel
    return root / ".opencode" / live_rel


@pytest.mark.golden
def test_plugin_install_matches_golden_tree(tmp_path: Path) -> None:
    """Rendered project-scope OpenCode plugin tree matches fixture bytes."""
    install_plugin(tmp_path, persist_manifest=False)

    expected_files = _walk_files(_FIXTURE_DIR)
    assert expected_files, "golden fixture is empty - regeneration mistake?"

    live_files = _walk_files(tmp_path)
    expected_rels = {p.relative_to(_FIXTURE_DIR) for p in expected_files}
    live_rels = {
        fixture_rel
        for path in live_files
        if (fixture_rel := _fixture_rel_for_live_path(path, tmp_path)) is not None
    }
    assert live_rels == expected_rels

    for expected_path in expected_files:
        rel = expected_path.relative_to(_FIXTURE_DIR)
        live_path = _live_path_for_fixture_rel(rel, tmp_path)
        assert live_path.read_bytes() == expected_path.read_bytes(), (
            f"file {rel} drifted from golden fixture. "
            "If this is intentional, regenerate the fixture."
        )


@pytest.mark.golden
def test_plugin_install_two_renders_byte_stable(tmp_path: Path) -> None:
    """Two OpenCode renders of the same registry produce identical bytes."""
    target_a = tmp_path / "a"
    target_b = tmp_path / "b"
    install_plugin(target_a, persist_manifest=False)
    install_plugin(target_b, persist_manifest=False)

    files_a = _walk_files(target_a)
    files_b = _walk_files(target_b)
    rels_a = {
        fixture_rel
        for path in files_a
        if (fixture_rel := _fixture_rel_for_live_path(path, target_a)) is not None
    }
    rels_b = {
        fixture_rel
        for path in files_b
        if (fixture_rel := _fixture_rel_for_live_path(path, target_b)) is not None
    }
    assert rels_a == rels_b
    for rel in rels_a:
        path_a = _live_path_for_fixture_rel(rel, target_a)
        path_b = _live_path_for_fixture_rel(rel, target_b)
        assert path_a.read_bytes() == path_b.read_bytes(), f"non-deterministic file: {rel}"
