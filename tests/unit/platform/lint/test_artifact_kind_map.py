"""The artifact-placement contract has one kind-to-subdirectory map, and every reader uses it.

Requirement rows proved here, by id:

- ``LINT-004``: the EAWF023 placement lint reads the one canonical kind map, so a kind
  the map carries is a sub-directory the lint accepts, and the second declaration the
  lint used to hold is gone.
- ``LINT-035``: the map has exactly one declaration in the source tree, and adding a kind
  is one edit: the placement lint, the draft promoter and both artifact path types accept
  the new kind with nothing else changed.

The one-declaration check is a scan of the source tree rather than an assertion about
named symbols, because the defect it guards against was a second literal nobody named:
the lint's own sub-directory set, the promoter's router and the generic path pattern each
restated the list, and a kind added to one of them reddened an unrelated test instead of
failing the lint.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from eawf.kernel.spec.common import ARTIFACT_KIND_SUBDIR, ArtifactPathStr
from eawf.platform.lint.eawf023_artifact_placement import check_artifact_path

SRC_ROOT = Path(__file__).resolve().parents[4] / "src" / "eawf"


#: The kind the one-edit cases add. No shipped kind uses it.
NEW_KIND = "notebook"
NEW_SUBDIR = "notebooks"


class _AnyArtifact(BaseModel):
    path: ArtifactPathStr


def _string_constants(node: ast.AST) -> list[str]:
    """Return the string constants directly inside a container literal."""
    if isinstance(node, ast.Dict):
        items = [*node.keys, *node.values]
    elif isinstance(node, ast.Set | ast.List | ast.Tuple):
        items = list(node.elts)
    else:
        return []
    return [i.value for i in items if isinstance(i, ast.Constant) and isinstance(i.value, str)]


def declarations(root: Path, subdirs: frozenset[str]) -> list[str]:
    """Return ``path:line`` of every literal in ``root`` that restates the sub-directory list.

    A container literal counts when it carries most of ``subdirs``; a string counts
    when it spells most of them as a ``|`` alternation, the shape a hand-written path
    pattern takes. A minority does not count, because several sub-directories share
    their name with a state collection (``research``, ``decisions``, ``incidents``)
    and lists of those collections are not declarations of the placement map.
    """
    found: list[tuple[Path, int]] = []
    majority = len(subdirs) // 2 + 1
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = set(_string_constants(node)) & subdirs
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
                names = set(re.findall(r"[a-z]+", node.value)) & subdirs
            if len(names) >= majority:
                found.append((path, node.lineno))
    return [f"{path.relative_to(root.parent.parent)}:{line}" for path, line in sorted(set(found))]


# ---------- LINT-004: the placement lint reads the one map ----------


@pytest.mark.parametrize("kind", sorted(ARTIFACT_KIND_SUBDIR))
def test_lint_004_the_placement_lint_accepts_every_subdirectory_the_map_declares(
    kind: str,
) -> None:
    """A dated artifact under any mapped sub-directory is clean under EAWF023."""
    path = f".ea/artifacts/{ARTIFACT_KIND_SUBDIR[kind]}/2026-09-29-topic.md"
    assert check_artifact_path(path) is None


def test_lint_004_the_lint_names_the_map_values_when_it_refuses() -> None:
    """The refusal lists exactly the map's sub-directories: the lint has no list of its own."""
    violation = check_artifact_path(".ea/artifacts/notes/2026-09-29-topic.md")
    assert violation is not None
    allowed = ", ".join(sorted(ARTIFACT_KIND_SUBDIR.values()))
    assert violation.reason.endswith(f"(allowed: {allowed})")


def test_lint_004_every_committed_subdirectory_is_a_map_value() -> None:
    """The sub-directories actually committed are all declared, so none is filed by habit."""
    committed = {
        path.name
        for path in (SRC_ROOT.parent.parent / ".ea" / "artifacts").iterdir()
        if path.is_dir() and path.name != "rendered"
    }
    assert committed
    assert committed <= set(ARTIFACT_KIND_SUBDIR.values())


# ---------- LINT-035: one declaration, one edit ----------


def test_lint_035_the_source_tree_declares_the_map_exactly_once() -> None:
    """Only ``kernel/spec/common.py`` restates the sub-directory list."""
    found = declarations(SRC_ROOT, frozenset(ARTIFACT_KIND_SUBDIR.values()))
    assert len(found) == 1, found
    assert found[0].startswith("src/eawf/kernel/spec/common.py:")


def test_lint_035_the_scan_finds_a_second_declaration(tmp_path: Path) -> None:
    """The scan has teeth: a restated set and a hand-written pattern are each found."""
    package = tmp_path / "src" / "eawf"
    package.mkdir(parents=True)
    (package / "lint.py").write_text(
        'SUBDIRS = frozenset({"audits", "research", "plans", "reviews", "evidence"})\n'
        'PATTERN = r"^\\.ea/artifacts/(audits|research|plans|reviews|evidence)/"\n'
        'COLLECTIONS = ["research", "decisions", "incidents", "hypotheses"]\n',
        encoding="utf-8",
    )
    found = declarations(package, frozenset(ARTIFACT_KIND_SUBDIR.values()))
    assert found == ["src/eawf/lint.py:1", "src/eawf/lint.py:2"]


def test_lint_035_the_scan_of_an_empty_tree_finds_nothing(tmp_path: Path) -> None:
    """The empty boundary: no source, no declaration."""
    assert declarations(tmp_path, frozenset(ARTIFACT_KIND_SUBDIR.values())) == []


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".ea/artifacts/audits/topic.md",
        ".ea/artifacts/audits/2026-09-29-topic.txt",
        ".ea/artifacts/2026-09-29-topic.md",
        "docs/audits/2026-09-29-topic.md",
    ],
)
def test_lint_035_the_generic_path_type_refuses_what_the_map_does_not_place(value: str) -> None:
    """Empty, undated, wrong suffix, loose-root and outside-the-tree paths are refused."""
    with pytest.raises(ValidationError):
        _AnyArtifact(path=value)


def test_lint_035_the_generic_path_type_refuses_a_non_string() -> None:
    """The wrong-type boundary is refused at the model, not coerced."""
    with pytest.raises(ValidationError):
        _AnyArtifact(path=20260929)  # type: ignore[arg-type]
