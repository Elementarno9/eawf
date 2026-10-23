"""The phase release path stays deleted; the Milestone pipeline is the only one.

``eawf release pipeline`` once took ``--phase`` and checked the merged
phase's wave pins on main through the wave-SHA helpers, and a post-merge
workflow printed that command. The native model releases Milestones, so
these checks red when the option, the request field, the helper module, an
import of it, or the workflow returns.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted wave-SHA helper module, by dotted name.
WAVE_SHA_MODULE: Final = "eawf.runtime.vcs.wave_sha"

#: The deleted paths, repo-relative.
DELETED_PATHS: Final = (
    "src/eawf/runtime/vcs/wave_sha.py",
    ".github/workflows/phase-release.yaml",
)


def _imported_modules(path: Path) -> set[str]:
    """Return every absolute dotted name *path* imports, module or attribute.

    ``from a import b`` yields both ``a`` and ``a.b`` because ``b`` may be a
    submodule. Relative imports are resolved against the file's package
    when the file lives under ``src/``.

    Args:
        path: A Python source file under :data:`REPO`.

    Returns:
        The dotted names the file imports at any nesting level.

    Raises:
        SyntaxError: *path* is not valid Python.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(REPO)
    package = list(rel.parts[1:-1]) if rel.parts[0] == "src" else []
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package[: len(package) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _imports_wave_sha(path: Path) -> bool:
    """Return whether *path* imports the wave-SHA module or a name from it."""
    return any(
        name == WAVE_SHA_MODULE or name.startswith(f"{WAVE_SHA_MODULE}.")
        for name in _imported_modules(path)
    )


def test_release_pipeline_takes_no_phase(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from eawf.surfaces.cli.app import app
    from eawf.surfaces.cli.commands import release_pipeline as cli_module
    from eawf.workflow.release import pipeline_host as host_module
    from eawf.workflow.release.pipeline import PipelineOptions

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a --phase run must stop at the option parse")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(host_module, "GitHubReleaseHost", _refuse)
    monkeypatch.setattr(cli_module, "_daemon_rpc", _refuse)
    result = CliRunner().invoke(app, ["release", "pipeline", "0.7.0.dev4", "--phase", "P35"])
    assert result.exit_code == 2, result.output
    assert "No such option" in result.output
    assert "phase_id" not in {field.name for field in dataclasses.fields(PipelineOptions)}


def test_wave_sha_and_phase_workflow_are_gone() -> None:
    assert [path for path in DELETED_PATHS if (REPO / path).exists()] == []
    importers = [
        str(path.relative_to(REPO))
        for top in ("src", "tools", "tests")
        for path in sorted((REPO / top).rglob("*.py"))
        if _imports_wave_sha(path)
    ]
    assert importers == []


def test_commit_identity_digest_left_wave_sha() -> None:
    diff_base = importlib.import_module("eawf.runtime.vcs.diff_base")
    repair = importlib.import_module("eawf.observability.doctor.repair")
    assert repair.commit_identity_digest is diff_base.commit_identity_digest
    assert not _imports_wave_sha(REPO / "src" / "eawf" / "observability" / "doctor" / "repair.py")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", False),
        ("import eawf.runtime.vcs.wave_sha\n", True),
        ("from eawf.runtime.vcs import wave_sha\n", True),
        ("from eawf.runtime.vcs.wave_sha import derive_diff_base\n", True),
        ("def f():\n    from eawf.runtime.vcs.wave_sha import X\n", True),
        ("from eawf.runtime.vcs import wave_sha_index\n", False),
        ("from eawf.runtime.vcs.diff_base import derive_diff_base\n", False),
    ],
)
def test_imports_wave_sha_flags_each_import_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected: bool
) -> None:
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    path = tmp_path / "tests" / "probe.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")
    assert _imports_wave_sha(path) is expected


def test_imports_wave_sha_raises_on_invalid_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    path = tmp_path / "tests" / "broken.py"
    path.parent.mkdir()
    path.write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _imports_wave_sha(path)
