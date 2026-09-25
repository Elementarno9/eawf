"""Verdict pin for the dropped EviBound rung-2 -> rung-3 text-claim chain.

The chain (``eawf.workflow.evidence.chain`` driving
``eawf.workflow.evidence.rung3``) was built and unit-tested but never reached
by a production caller: the research campaign promotes briefs through the
rung-1 gate only, and nothing bound a live juror ballot or read a rung-3
outcome. It was dropped rather than wired, so the verdict this suite holds is
"no source module defines, imports or names any chain symbol". Re-adding a
chain module or a reference to one of its symbols under ``src/`` reds here,
which forces the next author to supply the production producer and consumer
together instead of shipping idle substrate again.

The rung-2 scorer is not part of the drop: the jury-validation eval scores
cited claims through it, and the last test pins that live consumer so the
census cannot pass by deleting it too.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

import eawf.workflow.evidence as evidence_pkg

_SRC_ROOT = Path(__file__).resolve().parents[4] / "src"

_DROPPED_MODULES = frozenset({"eawf.workflow.evidence.chain", "eawf.workflow.evidence.rung3"})

_DROPPED_NAMES = frozenset(
    {
        "BallotFn",
        "DEFAULT_JUROR_COUNT",
        "NumericClaimError",
        "Rung3ConveneError",
        "Rung3Outcome",
        "build_juror_prompt",
        "convene_entailment_jury",
        "drive_text_claim_chain",
        "escalate_to_rung3",
        "jury_outcome_to_verdict",
        "parse_juror_ballot",
    }
)


def _module_references(tree: ast.AST) -> set[str]:
    """Return every module path an import statement in *tree* names."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _name_references(tree: ast.AST) -> set[str]:
    """Return every identifier *tree* binds, loads, imports or defines."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def chain_census(root: Path) -> list[str]:
    """List every chain reference under *root* as ``<relative path>:<symbol>``.

    Args:
        root: Directory whose ``*.py`` files are parsed.

    Returns:
        Sorted offender rows; empty when no file names a dropped module or
        symbol, including when *root* holds no Python file.

    Raises:
        FileNotFoundError: When *root* does not exist, so a mistyped root
            cannot pass the census vacuously.
        SyntaxError: When a file under *root* does not parse.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"census root is not a directory: {root}")
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(root).as_posix()
        hits = (_module_references(tree) & _DROPPED_MODULES) | (
            _name_references(tree) & _DROPPED_NAMES
        )
        offenders.extend(f"{rel}:{hit}" for hit in sorted(hits))
    return offenders


def test_chain_census_src_finds_no_chain_symbol() -> None:
    """The dropped verdict: no source module names the chain."""
    assert chain_census(_SRC_ROOT) == []


@pytest.mark.parametrize("module", sorted(_DROPPED_MODULES))
def test_chain_modules_not_importable(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_chain_evidence_package_exports_no_chain_symbol() -> None:
    assert _DROPPED_NAMES.isdisjoint(evidence_pkg.__all__)
    assert not any(hasattr(evidence_pkg, name) for name in _DROPPED_NAMES)


def test_chain_census_rung2_keeps_a_live_consumer() -> None:
    """Rung-2 stays: a module outside the evidence package imports it."""
    evidence_dir = _SRC_ROOT / "eawf" / "workflow" / "evidence"
    consumers = [
        path
        for path in _SRC_ROOT.rglob("*.py")
        if evidence_dir not in path.parents
        and "eawf.workflow.evidence.rung2"
        in _module_references(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert consumers


def test_chain_census_flags_reintroduced_import(tmp_path: Path) -> None:
    """Gate-fire: a re-added import of a dropped module is reported."""
    (tmp_path / "wiring.py").write_text(
        "from eawf.workflow.evidence.rung3 import Rung3Outcome\n", encoding="utf-8"
    )
    assert chain_census(tmp_path) == [
        "wiring.py:Rung3Outcome",
        "wiring.py:eawf.workflow.evidence.rung3",
    ]


def test_chain_census_flags_redefined_symbol(tmp_path: Path) -> None:
    """Gate-fire: re-defining a chain symbol in a new module is reported."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "driver.py").write_text(
        "async def drive_text_claim_chain() -> None:\n    return None\n", encoding="utf-8"
    )
    assert chain_census(tmp_path) == ["pkg/driver.py:drive_text_claim_chain"]


def test_chain_census_flags_attribute_access(tmp_path: Path) -> None:
    (tmp_path / "caller.py").write_text(
        "import eawf.workflow.evidence as ev\n\nev.escalate_to_rung3\n", encoding="utf-8"
    )
    assert chain_census(tmp_path) == ["caller.py:escalate_to_rung3"]


def test_chain_census_ignores_lookalike_names(tmp_path: Path) -> None:
    """A distinct identifier sharing a substring is not a chain reference."""
    (tmp_path / "clarity.py").write_text(
        "ClarityBallotFn = int\nDEFAULT_CLARITY_JUROR_COUNT = 3\n", encoding="utf-8"
    )
    assert chain_census(tmp_path) == []


def test_chain_census_empty_root(tmp_path: Path) -> None:
    assert chain_census(tmp_path) == []


def test_chain_census_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="census root is not a directory"):
        chain_census(tmp_path / "absent")


def test_chain_census_unparseable_file_raises(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("def (:\n", encoding="utf-8")
    with pytest.raises(SyntaxError):
        chain_census(tmp_path)
