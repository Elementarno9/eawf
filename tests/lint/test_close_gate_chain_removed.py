"""The wave close-gate and spec-jury chain stays deleted.

The chain had no production caller, so it was removed with its gate,
its tests and every module that imported it. These checks red when a
chain module, an import of one, or the gate that exercised it returns.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Final

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO: Final = Path(__file__).resolve().parents[2]

#: The deleted chain, by dotted module name.
CHAIN_MODULES: Final = (
    "eawf.workflow.verify.dispatch_close",
    "eawf.workflow.verify.oracle",
    "eawf.workflow.dispatch.verdict",
    "eawf.workflow.dispatch.spec_jury",
    "eawf.observability.eval.cross_vendor_jury",
)

#: The deleted chain, by repo-relative source path.
CHAIN_PATHS: Final = tuple(f"src/{name.replace('.', '/')}.py" for name in CHAIN_MODULES)

#: The modules that imported the chain before it was deleted.
FORMER_IMPORTERS: Final = (
    "src/eawf/runtime/daemon/server.py",
    "src/eawf/workflow/verify/__init__.py",
    "src/eawf/observability/eval/__init__.py",
    "src/eawf/workflow/dispatch/audit_evidence.py",
    "src/eawf/workflow/verify/readiness.py",
)

#: The idle-contract tools that named chain module paths.
IDLE_CONTRACT_TOOLS: Final = ("tools/idle_contract_gate.py", "tools/idle_contract_wiring.py")


def _imported_modules(path: Path) -> set[str]:
    """Return every absolute dotted name *path* imports, module or attribute.

    ``from a import b`` yields both ``a`` and ``a.b`` because ``b`` may be a
    submodule. Relative imports are resolved against the file's package
    when the file lives under ``src/``.

    Args:
        path: A Python source file.

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


def _chain_imports(path: Path) -> list[str]:
    """Return the chain modules *path* imports, sorted."""
    return sorted(set(CHAIN_MODULES) & _imported_modules(path))


def test_close_gate_chain_modules_are_gone() -> None:
    present = [path for path in CHAIN_PATHS if (REPO / path).exists()]
    assert present == []
    importers = {
        str(path.relative_to(REPO)): hits
        for top in ("src", "tools", "tests")
        for path in sorted((REPO / top).rglob("*.py"))
        if (hits := _chain_imports(path))
    }
    assert importers == {}


def test_no_gate_or_floor_names_the_deleted_chain() -> None:
    config = yaml.safe_load((REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook_ids = {hook["id"] for repo in config["repos"] for hook in repo["hooks"]}
    assert "jury-discrimination-gate" not in hook_ids
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert [path for path in CHAIN_PATHS if path in pyproject] == []


def test_live_importers_of_the_chain_are_rewired() -> None:
    rewired = {
        path: _chain_imports(REPO / path) for path in FORMER_IMPORTERS if (REPO / path).exists()
    }
    assert rewired == {path: [] for path in rewired}
    assert not (REPO / "tools" / "jury_discrimination_gate.py").exists()
    for tool in IDLE_CONTRACT_TOOLS:
        text = (REPO / tool).read_text(encoding="utf-8")
        named = [path for path in CHAIN_PATHS if path in text]
        named += [name for name in CHAIN_MODULES if name in text]
        assert named == [], tool
    importlib.import_module("eawf.runtime.daemon.server")


def test_imported_modules_resolves_from_imports() -> None:
    names = _imported_modules(REPO / "src" / "eawf" / "workflow" / "verify" / "__init__.py")
    assert "eawf.workflow.verify.readiness" in names
    assert "eawf.workflow.verify.readiness.compute" in names


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", []),
        ("import eawf.workflow.verify.oracle\n", ["eawf.workflow.verify.oracle"]),
        (
            "from eawf.observability.eval import cross_vendor_jury\n",
            ["eawf.observability.eval.cross_vendor_jury"],
        ),
        (
            "def f():\n    from eawf.workflow.dispatch.verdict import X\n",
            ["eawf.workflow.dispatch.verdict"],
        ),
        ("from eawf.workflow.dispatch import verdict_schema\n", []),
    ],
)
def test_chain_imports_flags_each_import_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str, expected: list[str]
) -> None:
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    path = tmp_path / "tests" / "probe.py"
    path.parent.mkdir()
    path.write_text(source, encoding="utf-8")
    assert _chain_imports(path) == expected


def test_chain_imports_raises_on_invalid_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(f"{__name__}.REPO", tmp_path)
    path = tmp_path / "tests" / "broken.py"
    path.parent.mkdir()
    path.write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _chain_imports(path)
