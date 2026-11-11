"""Tests that read this repository's committed ``.ea`` carry ``repo_state``.

A state-only change must not red the product suite, so every test module
that reads the repository's own ``.ea`` tree wears the ``repo_state``
marker, CI deselects that marker on a draft pull request's fast lane and
selects it in the full configuration, and the pre-push hook runs the
marked set before every push.

A module reads the committed tree when its AST names ``REPO_EA_DIR`` (or a
name bound to such a read, imported from another test module), joins a
repository-root path with a ``.ea`` segment by ``/`` or ``joinpath``, or
hands a repository-root path to a library entry point that reads ``.ea``
under the root it is given (:data:`LIBRARY_READERS`). A repository-root
path is ``Path(__file__)`` lifted by ``.parent`` or ``.parents[N]`` exactly
up to the repository root, or a name bound to one, including one imported
from another module under ``tests/``.
"""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from tests.lint.test_ci_workflow_shape import _evaluate, _render

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]
_TESTS_DIR: Final = "tests"
_MARKER: Final = "repo_state"
_EA_SEGMENT: Final = ".ea"
_EA_DIR_NAME: Final = "REPO_EA_DIR"
_CI_WORKFLOW: Final = _REPO_ROOT / ".github" / "workflows" / "ci.yaml"
_PRE_COMMIT: Final = _REPO_ROOT / ".pre-commit-config.yaml"
_PYPROJECT: Final = _REPO_ROOT / "pyproject.toml"
_DESELECT: Final = f"not {_MARKER}"
_TOOLS_DIR: Final = "tools"
#: Packages whose modules a test imports by repository path; their
#: constants and bindings are resolved from source.
_SOURCE_PACKAGES: Final = (_TESTS_DIR, _TOOLS_DIR)
_TEST_IMPORT: Final = re.compile(rf"^\s*from\s+(\.|{_TESTS_DIR}\b|{_TOOLS_DIR}\b)", re.MULTILINE)
_LIBRARY_PACKAGE: Final = "eawf"

#: Library entry points that open files under ``<root>/.ea`` when a test
#: hands them a repository root: the rule source and its renders, layered
#: config, committed canary, dispatch and certification evidence, release
#: admission and readiness, and the scans that walk committed ``.ea`` paths.
#: A call through any other path (a tmp tree) is not a read.
LIBRARY_READERS: Final = frozenset(
    {
        "_checkpoint_config",
        "_policy_graph",
        "checkpoint_release_config",
        "committed_membership_refs",
        "compile_rule_graph",
        "compute_readiness",
        "load_canary_evidence",
        "load_canary_receipts",
        "load_dispatch_smoke_receipts",
        "load_retyped_triage",
        "load_rule_source",
        "membership_probe",
        "merge_config",
        "plan_gitignore_block",
        "plan_rule_projections",
        "prior_canary_evidence",
        "provider_probe",
        "read_dispatch_checks",
        "release_preflight",
        "release_tag",
        "rendered_rules_outcome",
        "resolve_enabled_profiles",
        "runtime_certifications",
        "scan_paths",
    }
)


# --- AST scan ----------------------------------------------------------------


@dataclass(frozen=True)
class _Bindings:
    """Names a module binds to a repository root or to a committed ``.ea`` read."""

    roots: frozenset[str]
    ea_reads: frozenset[str]
    reads: bool
    ea_paths: frozenset[str] = frozenset()


def _lift(node: ast.expr, roots: frozenset[str]) -> int | None:
    """Return how many directories above ``__file__`` *node* points, or ``None``.

    A bound root name returns -1 as a sentinel the caller reads as "already
    the repository root".
    """
    if isinstance(node, ast.Name):
        return -1 if node.id in roots else None
    if isinstance(node, ast.Call):
        func = node.func
        if (
            isinstance(func, ast.Name)
            and func.id == "Path"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "__file__"
        ):
            return 0
        if isinstance(func, ast.Attribute) and func.attr in {"resolve", "absolute"}:
            return _lift(func.value, roots)
        return None
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        inner = _lift(node.value, roots)
        return None if inner is None or inner < 0 else inner + 1
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        inner = _lift(node.value.value, roots)
        return None if inner is None or inner < 0 else inner + node.slice.value + 1
    return None


def _is_root(node: ast.expr, roots: frozenset[str], depth: int) -> bool:
    lifted = _lift(node, roots)
    return lifted == -1 or lifted == depth


def _is_ea_segment(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.split("/", 1)[0] == _EA_SEGMENT
    )


def _is_ea_path(node: ast.expr, ea_paths: frozenset[str]) -> bool:
    """Return whether *node* is a relative path that starts with ``.ea``.

    A ``.ea`` string, ``Path`` of one, such a path joined further, or a
    name bound to any of them, including one imported from a module.
    """
    if _is_ea_segment(node):
        return True
    if isinstance(node, ast.Name):
        return node.id in ea_paths
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Name)
            and func.id in {"Path", "PurePosixPath", "PurePath"}
            and len(node.args) == 1
            and _is_ea_path(node.args[0], ea_paths)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_ea_path(node.left, ea_paths)
    return False


def _is_ea_join(node: ast.AST, roots: frozenset[str], depth: int, ea_paths: frozenset[str]) -> bool:
    """Return whether *node* joins a repository-root path with a ``.ea`` path."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_ea_path(node.right, ea_paths) and _is_root(node.left, roots, depth)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "joinpath"
        and node.args
    ):
        return _is_ea_path(node.args[0], ea_paths) and _is_root(node.func.value, roots, depth)
    return False


def _reader_names(nodes: list[ast.AST]) -> frozenset[str]:
    """Return the local names the module binds to a :data:`LIBRARY_READERS` entry."""
    return frozenset(
        alias.asname or alias.name
        for node in nodes
        if isinstance(node, ast.ImportFrom)
        and (node.module or "").split(".", 1)[0] == _LIBRARY_PACKAGE
        and not node.level
        for alias in node.names
        if alias.name in LIBRARY_READERS
    )


def _is_reader_call(
    node: ast.AST, readers: frozenset[str], roots: frozenset[str], depth: int
) -> bool:
    """Return whether *node* hands a repository-root path to a library reader.

    A bare name must be imported from the library; an attribute call
    (``module.reader(...)``) is matched by the attribute alone.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    named = (isinstance(func, ast.Name) and func.id in readers) or (
        isinstance(func, ast.Attribute) and func.attr in LIBRARY_READERS
    )
    arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
    return named and any(_is_root(argument, roots, depth) for argument in arguments)


def _import_source(repo_root: Path, relative: Path, node: ast.ImportFrom) -> Path | None:
    """Return the file under ``tests/`` or ``tools/`` an import resolves to, or ``None``."""
    if node.level:
        base = relative.parent
        for _ in range(node.level - 1):
            base = base.parent
        parts = [*base.parts, *(node.module or "").split(".")]
    else:
        parts = (node.module or "").split(".")
    parts = [part for part in parts if part]
    if not parts or parts[0] not in _SOURCE_PACKAGES:
        return None
    module = Path(*parts)
    for candidate in (module.with_suffix(".py"), module / "__init__.py"):
        if (repo_root / candidate).is_file():
            return candidate
    return None


def _bindings(
    repo_root: Path,
    relative: Path,
    tree: ast.Module,
    cache: dict[Path, _Bindings],
) -> _Bindings:
    """Return the root and ``.ea``-read names *tree* binds, imports included.

    Args:
        repo_root: The repository the module belongs to.
        relative: The module's path relative to *repo_root*.
        tree: The module's parsed AST.
        cache: Bindings of modules already resolved, keyed by relative path.
    """
    depth = len(relative.parts)
    nodes = list(ast.walk(tree))
    roots: set[str] = set()
    ea_reads: set[str] = {_EA_DIR_NAME} if relative.parts[0] == _TESTS_DIR else set()
    ea_paths: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            _bind_imports(repo_root, relative, node, cache, roots, ea_reads, ea_paths)
    assignments = [
        (target.id, node.value)
        for node in nodes
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    ]
    root_names = _close_roots(assignments, roots, depth)
    path_names = _close_ea_paths(assignments, ea_paths)
    # Reads are located once by source position; an assignment binds a read
    # when one lies inside its value, which avoids re-walking every value.
    readers = _reader_names(nodes)
    joins = [
        _position(node)
        for node in nodes
        if _is_ea_join(node, root_names, depth, path_names)
        or _is_reader_call(node, readers, root_names, depth)
    ]
    uses = [(node.id, _position(node)) for node in nodes if isinstance(node, ast.Name)]
    changed = True
    while changed:
        changed = False
        hits = [*joins, *(position for name, position in uses if name in ea_reads)]
        for name, value in assignments:
            if name not in ea_reads and any(_within(value, hit) for hit in hits):
                ea_reads.add(name)
                changed = True
    return _Bindings(
        roots=root_names,
        ea_reads=frozenset(ea_reads),
        reads=bool(joins) or any(name in ea_reads for name, _ in uses),
        ea_paths=path_names,
    )


def _bind_imports(
    repo_root: Path,
    relative: Path,
    node: ast.ImportFrom,
    cache: dict[Path, _Bindings],
    roots: set[str],
    ea_reads: set[str],
    ea_paths: set[str],
) -> None:
    """Add to *roots*, *ea_reads* and *ea_paths* the names *node* imports bound to each."""
    source = _import_source(repo_root, relative, node)
    if source is None or source == relative:
        return
    imported = _module_bindings(repo_root, source, cache)
    for alias in node.names:
        bound = alias.asname or alias.name
        if alias.name in imported.roots:
            roots.add(bound)
        if alias.name in imported.ea_reads:
            ea_reads.add(bound)
        if alias.name in imported.ea_paths:
            ea_paths.add(bound)


def _close_roots(
    assignments: list[tuple[str, ast.expr]], roots: set[str], depth: int
) -> frozenset[str]:
    """Return *roots* grown by every name assigned a repository-root path."""
    grown = set(roots)
    changed = True
    while changed:
        changed = False
        frozen = frozenset(grown)
        for name, value in assignments:
            if name not in grown and _is_root(value, frozen, depth):
                grown.add(name)
                changed = True
    return frozenset(grown)


def _close_ea_paths(assignments: list[tuple[str, ast.expr]], ea_paths: set[str]) -> frozenset[str]:
    """Return *ea_paths* grown by every name assigned a ``.ea`` relative path."""
    grown = set(ea_paths)
    changed = True
    while changed:
        changed = False
        frozen = frozenset(grown)
        for name, value in assignments:
            if name not in grown and _is_ea_path(value, frozen):
                grown.add(name)
                changed = True
    return frozenset(grown)


def _position(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _within(value: ast.expr, position: tuple[int, int]) -> bool:
    start = (value.lineno, value.col_offset)
    end = (value.end_lineno or value.lineno, value.end_col_offset or value.col_offset)
    return start <= position < end


def _module_bindings(repo_root: Path, relative: Path, cache: dict[Path, _Bindings]) -> _Bindings:
    if relative not in cache:
        # A placeholder breaks an import cycle between two test helpers.
        cache[relative] = _Bindings(roots=frozenset(), ea_reads=frozenset(), reads=False)
        tree = ast.parse((repo_root / relative).read_text(encoding="utf-8"))
        cache[relative] = _bindings(repo_root, relative, tree, cache)
    return cache[relative]


def _is_marked(tree: ast.Module) -> bool:
    """Return whether a module-level ``pytestmark`` applies ``repo_state``."""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
        if value is not None and any(
            isinstance(child, ast.Attribute) and child.attr == _MARKER for child in ast.walk(value)
        ):
            return True
    return False


@dataclass(frozen=True)
class ScanRow:
    """One test module's verdict: whether it reads the committed tree, and is marked."""

    path: str
    reads: bool
    marked: bool


def scan_module(repo_root: Path, relative: Path, source: str) -> ScanRow:
    """Classify one test module by its source.

    Args:
        repo_root: The repository whose ``tests/`` resolves the module's imports.
        relative: The module's path relative to *repo_root*; its depth fixes
            which ``Path(__file__)`` lift reaches the repository root.
        source: The module's Python source.

    Returns:
        The module's :class:`ScanRow`.

    Raises:
        SyntaxError: *source* is not valid Python.
        ValueError: *relative* is empty or absolute.
    """
    return _scan(repo_root, relative, source, {})


def _scan(repo_root: Path, relative: Path, source: str, cache: dict[Path, _Bindings]) -> ScanRow:
    if not relative.parts or relative.is_absolute():
        raise ValueError(f"expected a repository-relative module path, got {relative!r}")
    # A read needs a ``.ea`` literal, a library reader or a name imported
    # from another test module, and a mark needs the marker name; a source
    # with none of them is never parsed.
    if not (
        _EA_SEGMENT in source
        or _MARKER in source
        or _TEST_IMPORT.search(source)
        or any(reader in source for reader in LIBRARY_READERS)
    ):
        return ScanRow(path=relative.as_posix(), reads=False, marked=False)
    tree = ast.parse(source)
    return ScanRow(
        path=relative.as_posix(),
        reads=_bindings(repo_root, relative, tree, cache).reads,
        marked=_is_marked(tree),
    )


def _test_modules(repo_root: Path) -> Iterator[Path]:
    for path in sorted((repo_root / _TESTS_DIR).rglob("test_*.py")):
        if "__pycache__" not in path.parts:
            yield path.relative_to(repo_root)


def scan_tree(repo_root: Path) -> list[ScanRow]:
    """Return a :class:`ScanRow` for every ``test_*.py`` module under ``tests/``."""
    # One cache for the whole tree: most modules import the same helpers.
    cache: dict[Path, _Bindings] = {}
    return [
        _scan(repo_root, relative, (repo_root / relative).read_text(encoding="utf-8"), cache)
        for relative in _test_modules(repo_root)
    ]


def _marked_modules() -> list[str]:
    return [row.path for row in scan_tree(_REPO_ROOT) if row.marked]


def test_repo_state_modules_marked() -> None:
    markers = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["markers"]
    assert any(line.split(":", 1)[0].strip() == _MARKER for line in markers)
    rows = scan_tree(_REPO_ROOT)
    unmarked = [row.path for row in rows if row.reads and not row.marked]
    assert unmarked == [], f"modules read the committed .ea without {_MARKER}: {unmarked}"
    # Boundary: the scan must see real readers, or an emptied scan passes vacuously.
    assert sum(row.reads for row in rows) >= 10


# --- CI deselection and the pre-push hook -------------------------------------


def _pytest_commands(script: str) -> list[list[str]]:
    """Return the argv of every ``pytest`` command in one ``run:`` script."""
    joined = script.replace("\\\n", " ")
    commands: list[list[str]] = []
    for line in joined.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        try:
            argv = shlex.split(stripped)
        except ValueError:
            continue
        if "pytest" in argv:
            commands.append(argv[argv.index("pytest") :])
    return commands


def _ci_steps(workflow: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def _marker_expression(argv: list[str]) -> str | None:
    """Return the last ``-m`` expression in *argv*; pytest keeps only the last."""
    expression: str | None = None
    for index, arg in enumerate(argv):
        if arg == "-m" and index + 1 < len(argv):
            expression = argv[index + 1]
        elif arg.startswith("-m") and len(arg) > 2:
            expression = arg[2:]
    return expression


def _default_conjuncts() -> list[str]:
    addopts = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["tool"]["pytest"][
        "ini_options"
    ]["addopts"]
    expression = _marker_expression(shlex.split(addopts))
    return [] if expression is None else [c.strip() for c in expression.split(" and ")]


#: Every event shape a ci.yaml run takes: a label, the event name and the
#: pull request's draft flag (null outside a pull request).
EVENT_SHAPES: Final[tuple[tuple[str, str, bool | None], ...]] = (
    ("draft pull request", "pull_request", True),
    ("ready pull request", "pull_request", False),
    ("push", "push", None),
    ("schedule", "schedule", None),
    ("workflow_dispatch", "workflow_dispatch", None),
)
_ENV_REFERENCE: Final = re.compile(r"\$\{\{\s*env\.([A-Za-z_]\w*)\s*\}\}")


def shape_context(event: str, draft: bool | None) -> dict[str, object]:
    """Return the Actions context of one event shape, its changes job reporting code.

    Args:
        event: The ``github.event_name`` of the run.
        draft: The pull request's draft flag; ``None`` outside a pull request.

    Returns:
        The context keys the workflow's env and job conditions read.
    """
    return {
        "github.event_name": event,
        "github.event.pull_request.draft": draft,
        "needs.changes.result": "success",
        "needs.changes.outputs.code": "true",
    }


def is_fast_lane(context: dict[str, object]) -> bool:
    """Return whether *context* is a draft pull request, the only run deselecting the marker."""
    return (
        context["github.event_name"] == "pull_request"
        and context["github.event.pull_request.draft"] is True
    )


def _workflow_env(workflow: dict[str, Any], context: dict[str, object]) -> dict[str, str]:
    return {name: _render(str(value), context) for name, value in workflow.get("env", {}).items()}


def _with_env(text: str, env: dict[str, str]) -> str:
    """Substitute each ``${{ env.NAME }}`` the runner would; an undefined name raises KeyError."""
    return _ENV_REFERENCE.sub(lambda match: env[match.group(1)], text)


def selection_problems(
    workflow: dict[str, Any], default_conjuncts: list[str], context: dict[str, object]
) -> list[str]:
    """Return every CI pytest call whose ``repo_state`` selection is wrong for *context*.

    A draft pull request's fast lane must deselect the marker; every other
    run is the full configuration and must select it. A command-line ``-m``
    replaces the ini ``addopts`` one rather than adding to it, so each call
    must also keep the default conjuncts it does not explicitly invert (the
    eval step selects ``eval`` on purpose). A call without ``-m`` runs the
    ini default.

    Args:
        workflow: The parsed workflow document.
        default_conjuncts: The ``and`` conjuncts of the ini ``-m`` expression.
        context: The event shape, as :func:`shape_context` builds it.

    Returns:
        One line per offending call; empty when every call selects as it should.

    Raises:
        KeyError: *workflow* has no ``jobs`` table, or a call names an
            ``env`` value the workflow does not define.
    """
    deselect = is_fast_lane(context)
    env = _workflow_env(workflow, context)
    problems: list[str] = []
    for step in _ci_steps(workflow):
        name = step.get("name", "<unnamed>")
        run = step.get("run")
        if not isinstance(run, str):
            continue
        if "just " in run and "pytest" not in run:
            addopts = _with_env(str(step.get("env", {}).get("PYTEST_ADDOPTS", "")), env)
            expressions = [_marker_expression(shlex.split(addopts))]
        else:
            expressions = [
                _marker_expression(argv) for argv in _pytest_commands(_with_env(run, env))
            ]
        for expression in expressions:
            conjuncts = (
                default_conjuncts
                if expression is None
                else [c.strip() for c in expression.split(" and ")]
            )
            if deselect and _DESELECT not in conjuncts:
                problems.append(f"{name}: the fast lane runs pytest without {_DESELECT!r}")
            if not deselect and _DESELECT in conjuncts:
                problems.append(f"{name}: the full configuration deselects {_MARKER!r}")
            selected = {c for c in conjuncts if not c.startswith("not ")}
            lost = [c for c in default_conjuncts if c not in conjuncts and c[4:] not in selected]
            if lost:
                problems.append(f"{name}: -m drops the default {lost}")
    return problems


def prepush_problems(config: dict[str, Any]) -> list[str]:
    """Return why the pre-commit config does not run ``pytest -m repo_state`` at push.

    Args:
        config: The parsed ``.pre-commit-config.yaml``.

    Returns:
        Empty when a pre-push hook runs the marked set; one line otherwise.
    """
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            if f"pytest -m {_MARKER}" in str(hook.get("entry", "")) and "pre-push" in hook.get(
                "stages", []
            ):
                return []
    return [f"no pre-push hook runs pytest -m {_MARKER}"]


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def called_workflows(workflow: dict[str, Any]) -> list[str]:
    """Return the repository workflows *workflow*'s jobs call, as repo-relative paths.

    Args:
        workflow: The parsed workflow document.

    Returns:
        Each local ``uses: ./...`` target in job order; a remote reusable
        workflow (``owner/repo/...@ref``) is not this repository's to check.

    Raises:
        KeyError: *workflow* has no ``jobs`` table.
    """
    return [
        uses.removeprefix("./")
        for job in workflow["jobs"].values()
        if isinstance(uses := job.get("uses"), str) and uses.startswith("./")
    ]


def test_ci_deselects_and_prepush_runs() -> None:
    workflow = _load_yaml(_CI_WORKFLOW)
    called = called_workflows(workflow)
    # Boundary: ci.yaml calls the console replay, so the walk must reach it.
    assert ".github/workflows/console-replay.yaml" in called
    callers = {
        str(job["uses"]).removeprefix("./"): str(job.get("if", "true"))
        for job in workflow["jobs"].values()
        if "uses" in job
    }
    defaults = _default_conjuncts()
    calls: list[list[str]] = []
    checked: set[str] = set()
    for label, event, draft in EVENT_SHAPES:
        context = shape_context(event, draft)
        assert selection_problems(workflow, defaults, context) == [], label
        # A called workflow runs only where its calling job does, and sees
        # none of the caller's env, so it is judged on those shapes alone.
        for path in called:
            if _evaluate(callers[path], context):
                checked.add(path)
                document = _load_yaml(_REPO_ROOT / path)
                assert selection_problems(document, defaults, context) == [], (label, path)
    for document in (workflow, *(_load_yaml(_REPO_ROOT / path) for path in called)):
        calls += [
            argv
            for step in _ci_steps(document)
            for argv in _pytest_commands(str(step.get("run", "")))
        ]
    assert prepush_problems(_load_yaml(_PRE_COMMIT)) == []
    # Boundary: every called workflow ran under some shape, and the walk
    # found real pytest calls, or an empty parse passes.
    assert checked == set(called)
    assert len(calls) >= 5


def _one_call_workflow(marker: str, env: str | None = None) -> dict[str, Any]:
    workflow: dict[str, Any] = {
        "jobs": {"t": {"steps": [{"name": "s", "run": f'uv run pytest -m "{marker}" -n0'}]}}
    }
    if env is not None:
        workflow["env"] = {"REPO_STATE_DESELECT": env}
    return workflow


_FAST_LANE_ENV: Final = (
    "${{ github.event_name == 'pull_request' && github.event.pull_request.draft"
    " && ' and not repo_state' || '' }}"
)


def test_selection_follows_the_event_shape() -> None:
    computed = _one_call_workflow("not eval${{ env.REPO_STATE_DESELECT }}", _FAST_LANE_ENV)
    for label, event, draft in EVENT_SHAPES:
        assert selection_problems(computed, ["not eval"], shape_context(event, draft)) == [], label
    assert is_fast_lane(shape_context("pull_request", True))
    assert not is_fast_lane(shape_context("pull_request", False))
    assert not is_fast_lane(shape_context("push", None))


def test_selection_fire_proof() -> None:
    draft = shape_context("pull_request", True)
    ready = shape_context("pull_request", False)
    # The literal deselection CI carried reds the full configuration and
    # passes only the fast lane.
    literal = _one_call_workflow("not eval and not repo_state")
    assert selection_problems(literal, ["not eval"], ready) == [
        "s: the full configuration deselects 'repo_state'"
    ]
    assert selection_problems(literal, ["not eval"], draft) == []
    # A bare default reds the fast lane.
    assert selection_problems(_one_call_workflow("not eval"), ["not eval"], draft) == [
        "s: the fast lane runs pytest without 'not repo_state'"
    ]
    # Dropping the default conjunct reds every shape; eval selected on purpose does not.
    assert selection_problems(_one_call_workflow("not slow"), ["not eval"], ready) == [
        "s: -m drops the default ['not eval']"
    ]
    assert selection_problems(_one_call_workflow("eval"), ["not eval"], ready) == []
    # A call without -m runs the ini default.
    bare = {"jobs": {"t": {"steps": [{"name": "s", "run": "uv run pytest -n0"}]}}}
    assert selection_problems(bare, ["not eval"], ready) == []
    assert selection_problems(bare, ["not eval"], draft) != []
    # Empty: no steps, no problems.
    assert selection_problems({"jobs": {}}, ["not eval"], draft) == []


def test_selection_error_paths() -> None:
    undefined = _one_call_workflow("not eval${{ env.REPO_STATE_DESELECT }}")
    with pytest.raises(KeyError, match="REPO_STATE_DESELECT"):
        selection_problems(undefined, ["not eval"], shape_context("push", None))
    with pytest.raises(KeyError, match="jobs"):
        selection_problems({}, ["not eval"], shape_context("push", None))


def test_called_workflows_lists_local_reusable_workflows_only() -> None:
    workflow = {
        "jobs": {
            "a": {"uses": "./.github/workflows/x.yaml"},
            "b": {"uses": "owner/repo/.github/workflows/y.yaml@v1"},
            "c": {"steps": [{"uses": "./local-action"}]},
            "d": {"uses": None},
        }
    }
    assert called_workflows(workflow) == [".github/workflows/x.yaml"]
    assert called_workflows({"jobs": {}}) == []


# --- fire proof ----------------------------------------------------------------


_UNMARKED_FIXTURE: Final = (
    "from __future__ import annotations\n"
    "from tests.conftest import REPO_EA_DIR\n"
    "def test_reads() -> None:\n"
    "    assert (REPO_EA_DIR / 'state.json').exists()\n"
)


_READER_FIXTURE: Final = (
    "from __future__ import annotations\n"
    "from pathlib import Path\n"
    "{imports}\n"
    "_ROOT = Path(__file__).resolve().parents[2]\n"
    "def test_reads(tmp_path: Path) -> None:\n"
    "    {call}\n"
)


def _reader_reads(imports: str, call: str) -> bool:
    fixture = Path(_TESTS_DIR) / "lint" / "test_unmarked_fixture.py"
    source = _READER_FIXTURE.format(imports=imports, call=call)
    return scan_module(_REPO_ROOT, fixture, source).reads


def test_marker_scan_fire_proof_library_reader() -> None:
    """A library reader handed the repository root reads the committed tree."""
    rules = "from eawf.platform.rules.loader import load_rule_source"
    # The unmarked shape the four rule-source modules carried.
    assert _reader_reads(rules, "load_rule_source(_ROOT)")
    assert _reader_reads(rules, "load_rule_source(root=_ROOT)")
    assert _reader_reads(rules, "load_rule_source(Path(__file__).resolve().parents[2])")
    assert _reader_reads(rules.replace("source", "source as lrs", 1), "lrs(_ROOT)")
    assert _reader_reads("from eawf.platform.rules import loader", "loader.load_rule_source(_ROOT)")
    # A tmp tree, a root at the wrong depth, a test-local namesake and a
    # library function outside the list are not reads.
    assert not _reader_reads(rules, "load_rule_source(tmp_path)")
    assert not _reader_reads(rules, "load_rule_source(Path(__file__).resolve().parents[1])")
    assert not _reader_reads(
        "from tests.helpers import load_rule_source", "load_rule_source(_ROOT)"
    )
    assert not _reader_reads("from eawf.kernel.state import load_state", "load_state(_ROOT)")
    # A module with no .ea literal and no test import still reaches the parse.
    assert ".ea" not in _READER_FIXTURE.format(imports=rules, call="load_rule_source(_ROOT)")


def test_marker_scan_fire_proof_imported_ea_path() -> None:
    """A root joined with an imported ``.ea`` path constant reads the committed tree."""
    catalog = "from tools.requirement_trace import CATALOG_PATH"
    # The unmarked shape test_requirement_trace_satisfied.py carried.
    assert _reader_reads(catalog, "(_ROOT / CATALOG_PATH).read_text()")
    assert _reader_reads(catalog.replace("PATH", "PATH as C", 1), "_ROOT.joinpath(C)")
    gate = "from tools.snapshot_pairing_gate import _STATE_RELPATH"
    assert _reader_reads(gate, "(_ROOT / _STATE_RELPATH).exists()")
    local = "from pathlib import PurePath\nREL = PurePath('.ea') / 'state.json'"
    assert _reader_reads(local, "(_ROOT / REL).exists()")
    # A tmp tree, a non-.ea constant and an unresolvable module are not reads.
    assert not _reader_reads(catalog, "(tmp_path / CATALOG_PATH).exists()")
    assert not _reader_reads("from tools.requirement_trace import main", "(_ROOT / main).exists()")
    assert not _reader_reads("from tools.absent import CATALOG_PATH", "(_ROOT / CATALOG_PATH)")


def test_library_readers_exist() -> None:
    """Every listed reader is still defined, so a rename cannot empty the rule."""
    defined = {
        node.name
        for path in (_REPO_ROOT / "src" / _LIBRARY_PACKAGE).rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert sorted(LIBRARY_READERS - defined) == []


def test_marker_scan_fire_proof(tmp_path: Path) -> None:
    fixture = Path(_TESTS_DIR) / "lint" / "test_unmarked_fixture.py"
    row = scan_module(_REPO_ROOT, fixture, _UNMARKED_FIXTURE)
    assert row.reads and not row.marked
    marked = scan_module(
        _REPO_ROOT,
        fixture,
        "import pytest\npytestmark = [pytest.mark.unit, pytest.mark.repo_state]\n"
        + _UNMARKED_FIXTURE,
    )
    assert marked.reads and marked.marked
    # The inline lift reaches the root only at the module's own depth.
    inline = "from pathlib import Path\nX = Path(__file__).resolve().parents[{n}] / '.ea'\n"
    assert scan_module(_REPO_ROOT, fixture, inline.format(n=2)).reads
    assert not scan_module(_REPO_ROOT, fixture, inline.format(n=1)).reads
    # A tmp tree's .ea and a .eawf sibling are not the committed tree.
    assert not scan_module(
        _REPO_ROOT, fixture, "def f(tmp_path):\n    return tmp_path / '.ea'\n"
    ).reads
    assert not scan_module(
        _REPO_ROOT, fixture, "from tests.conftest import REPO_ROOT\nX = REPO_ROOT / '.eawf'\n"
    ).reads

    # A copy of tests/ whose REPO_EA_DIR is an empty directory: collection
    # under the CI deselection must import every marked module cleanly and
    # select none of their tests.
    marked_modules = _marked_modules()
    assert marked_modules
    copy = tmp_path / "repo"
    shutil.copytree(
        _REPO_ROOT / _TESTS_DIR,
        copy / _TESTS_DIR,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    # The rest of the checkout is linked in so modules that read source or
    # docs still import; git stays out so the copy is its own main checkout.
    for entry in _REPO_ROOT.iterdir():
        if entry.name not in {_TESTS_DIR, _EA_SEGMENT, ".git", ".venv"}:
            (copy / entry.name).symlink_to(entry, target_is_directory=entry.is_dir())
    (copy / _EA_SEGMENT).mkdir()
    pythonpath = os.pathsep.join(
        part for part in (os.environ.get("PYTHONPATH", ""), str(_REPO_ROOT)) if part
    )
    expression = " and ".join([*_default_conjuncts(), _DESELECT])
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-m",
            expression,
            *marked_modules,
        ],
        cwd=copy,
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 5: every collected test was deselected, which is the point.
    assert result.returncode == 5, result.stdout + result.stderr
    selected = [line for line in result.stdout.splitlines() if "::" in line]
    assert selected == []
