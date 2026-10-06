"""No test or tool reads a non-promoted file under this repository's ``.ea/artifacts/``.

The artifact tree is machine-local: git carries only the promoted list in
:data:`eawf.kernel.store.commit_policy.PROMOTED_ARTIFACTS`, so a fresh clone
has nothing else there. A test that joins the repository root with any
other artifact path passes on the machine that wrote the artifact and fails
on every clone, CI included. Inputs a test pins live under
``tests/fixtures/artifacts/`` instead.

The scan is syntactic. It flags a path built from a repository-root
expression -- a name ending in ``root`` or anything derived from
``__file__`` -- by ``/`` or ``joinpath`` whose literal segments spell a path
under ``.ea/artifacts``, and a ``Path(...)`` literal under ``.ea/artifacts``
that a test later joins onto a checkout. Bare strings are not flagged: they
are path shapes handed to a lint or compared against recorded refs, not
reads.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Final

import pytest

from eawf.kernel.store.commit_policy import PROMOTED_ARTIFACTS

pytestmark = pytest.mark.unit

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]

#: The trees whose Python must not read local artifacts.
_SCANNED_TREES: Final = ("tests", "tools")

_ARTIFACT_ROOT: Final = ".ea/artifacts"

#: A name that holds a checkout root rather than a scratch directory.
_ROOT_NAME: Final = re.compile(r"(?i)^_?(repo_|src_)?root$")

#: The only artifact paths git carries, so the only ones a test may read.
_PROMOTED: Final = frozenset(row.pattern for row in PROMOTED_ARTIFACTS)

#: Modules allowed to build a non-promoted artifact path, each with the reason.
#: An entry is a writer, never a reader: it files fresh local evidence there.
_WRITERS: Final[dict[str, str]] = {
    "tests/tui/surfaces/tui/console/test_console_live_smoke.py": (
        "files the opt-in live run's frames into a local evidence directory; reads nothing"
    ),
}


def _is_repo_rooted(node: ast.expr) -> bool:
    """Return whether *node* evaluates to a checkout root or a path below one.

    Only bare names count: an attribute such as ``world.root`` is a scratch
    tree a fixture built, not this checkout.
    """
    return any(
        isinstance(child, ast.Name) and (child.id == "__file__" or _ROOT_NAME.match(child.id))
        for child in ast.walk(node)
    )


def _literal(node: ast.expr) -> str | None:
    """Return the string a constant segment spells, or None for a computed one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _flatten_division(node: ast.BinOp) -> tuple[ast.expr, list[ast.expr]]:
    """Return the base of a ``a / b / c`` chain and its segments in order."""
    segments: list[ast.expr] = []
    current: ast.expr = node
    while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
        segments.insert(0, current.right)
        current = current.left
    return current, segments


def _joined(segments: list[ast.expr]) -> str:
    """Join the leading literal segments, stopping at the first computed one."""
    parts: list[str] = []
    for segment in segments:
        text = _literal(segment)
        if text is None:
            break
        parts.append(text.strip("/"))
    return "/".join(part for part in parts if part)


def _reads_local_artifact(path: str) -> bool:
    """Return whether *path* names something under the artifact tree git drops."""
    under = path == _ARTIFACT_ROOT or path.startswith(f"{_ARTIFACT_ROOT}/")
    return under and path not in _PROMOTED


def artifact_reads(source: str) -> list[tuple[int, str]]:
    """Return ``(line, path)`` for every non-promoted artifact path *source* builds.

    Args:
        source: The Python module text to scan.

    Returns:
        One entry per offending expression, outermost division chains only.

    Raises:
        SyntaxError: When *source* is not valid Python.
    """
    found: list[tuple[int, str]] = []
    inner: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if id(node) in inner:
            continue
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            base, segments = _flatten_division(node)
            inner.update(id(child) for child in ast.walk(node) if child is not node)
            path = _joined(segments)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "joinpath"
        ):
            base = node.func.value
            path = _joined(list(node.args))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"Path", "PurePath", "PurePosixPath"}
            and node.args
        ):
            text = _literal(node.args[0])
            if text is not None and _reads_local_artifact(text.strip("/")):
                found.append((node.lineno, text.strip("/")))
            continue
        else:
            continue
        if _reads_local_artifact(path) and _is_repo_rooted(base):
            found.append((node.lineno, path))
    return sorted(found)


def _scanned_modules() -> list[str]:
    return sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for tree in _SCANNED_TREES
        for path in (_REPO_ROOT / tree).rglob("*.py")
    )


def test_no_test_or_tool_reads_a_non_promoted_artifact() -> None:
    offenders = [
        f"{module}:{line}: {path}"
        for module in _scanned_modules()
        if module not in _WRITERS
        for line, path in artifact_reads((_REPO_ROOT / module).read_text(encoding="utf-8"))
    ]
    assert offenders == [], (
        "a fresh clone has no local artifacts; move each input under tests/fixtures/artifacts/:\n"
        + "\n".join(offenders)
    )


def test_every_writer_exemption_names_a_module_that_still_builds_one() -> None:
    for module in _WRITERS:
        source = (_REPO_ROOT / module).read_text(encoding="utf-8")
        assert artifact_reads(source), f"{module} no longer builds an artifact path; drop it"


@pytest.mark.parametrize(
    "line",
    [
        '_REPO_ROOT / ".ea" / "artifacts" / "audits" / "x.md"',
        '_REPO_ROOT / ".ea/artifacts/evidence/x/y.json"',
        'REPO_ROOT / ".ea" / "artifacts"',
        'repo_root / ".ea" / "artifacts" / "evidence" / "d"',
        'Path(__file__).resolve().parents[4] / ".ea" / "artifacts" / "x.json"',
        'SRC_ROOT.parent.parent / ".ea" / "artifacts"',
        '_REPO_ROOT.joinpath(".ea", "artifacts", "plans", "x.md")',
        'Path(".ea/artifacts/evidence/x/y.json")',
    ],
)
def test_a_planted_non_promoted_read_reds(line: str) -> None:
    assert len(artifact_reads(f"x = {line}\n")) == 1


@pytest.mark.parametrize(
    "line",
    [
        'tmp_path / ".ea" / "artifacts" / "audits" / "x.md"',
        'world.root / ".ea/artifacts"',
        'repo / ".ea" / "artifacts" / "x.md"',
        '".ea/artifacts/audits/x.md"',
        '_REPO_ROOT / "tests" / "fixtures" / "artifacts" / "x.json"',
        '_REPO_ROOT / ".ea" / "state.json"',
        '_REPO_ROOT / ".ea" / "artifactsx"',
        "_REPO_ROOT / name",
        "",
    ],
)
def test_a_scratch_tree_a_string_or_another_path_stays_clean(line: str) -> None:
    assert artifact_reads(f"x = {line}\n" if line else "") == []


def test_each_promoted_path_may_be_read() -> None:
    for pattern in _PROMOTED:
        assert artifact_reads(f"x = _REPO_ROOT / {pattern!r}\n") == []


def test_a_computed_segment_after_the_tree_still_reds() -> None:
    """A path that reaches the tree and then varies is a read of the tree."""
    found = artifact_reads('x = _REPO_ROOT / ".ea" / "artifacts" / name\n')
    assert found == [(1, ".ea/artifacts")]


def test_a_module_that_does_not_parse_raises() -> None:
    with pytest.raises(SyntaxError):
        artifact_reads("x = (\n")
