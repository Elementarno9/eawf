"""Structural pins for the split ``state.*`` daemon method family.

The state-method module used to be one 5,286-line file. It is now a facade plus
nine collaborators, and these tests pin the three properties that make the split
worth having rather than a rename:

* every module under ``runtime/daemon/methods`` fits the EAWF010 budget on its
  own, so no member of the family is exempted by the pyproject grandfather list;
* no module in the family reaches into a sibling's private (underscore) names,
  so each collaborator's cross-module surface is a deliberate public API;
* the family's internal import graph is acyclic, so the layering is real and a
  future edit cannot quietly reintroduce the tangle the split removed.

They live under ``tests/daemon`` because the subject is the daemon method
package, and they read the source tree rather than the imported modules: the
properties are about the FILES, so a test that imported them would miss exactly
the regressions worth catching.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
METHODS_DIR = REPO_ROOT / "src" / "eawf" / "runtime" / "daemon" / "methods"
METHODS_PACKAGE = "eawf.runtime.daemon.methods"

#: The EAWF010 module-length budget every method module must fit.
MAX_LOC = 1400

#: Modules in this package that pre-date the split and carry their own
#: ``# noqa: EAWF010 <rationale>`` waiver. Each is a cohesive command surface
#: whose split is its own piece of work; the set is frozen here so a NEW module
#: cannot join it silently -- an addition has to edit this list in review.
WAIVERED_MODULES = frozenset({"agent.py", "fleet.py", "research.py"})


def _method_modules() -> list[Path]:
    """Return every Python module in the daemon method package."""
    return sorted(path for path in METHODS_DIR.glob("*.py"))


#: Private cross-module reaches that pre-date this split, each between a pair of
#: modules the split did not touch. They are frozen here rather than silently
#: tolerated: the gate below still refuses any NEW one, and shrinking this set is
#: its own piece of hygiene work on those two module pairs.
LEGACY_PRIVATE_SIBLING_IMPORTS = frozenset(
    {
        ("close.py", "close_evidence", "_digest"),
        ("close.py", "close_evidence", "_load_state"),
        ("close.py", "close_evidence", "_state_path"),
        ("spec_convert.py", "spec", "_cache_replay"),
        ("spec_convert.py", "spec", "_idempotent_replay"),
        ("spec_convert.py", "spec", "_publish"),
        ("spec_convert.py", "spec", "_validate_post_sync"),
    }
)


def _sibling_imports(tree: ast.Module, *, module_level_only: bool = False) -> list[tuple[str, str]]:
    """Return ``(sibling_module, imported_name)`` for each intra-package import.

    With *module_level_only* the scan is restricted to imports at module scope,
    which is the set that constrains import ORDER; a function-local import is a
    deliberately deferred edge that no import order has to satisfy.
    """
    nodes = tree.body if module_level_only else list(ast.walk(tree))
    pairs: list[tuple[str, str]] = []
    for node in nodes:
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        if not node.module.startswith(f"{METHODS_PACKAGE}."):
            continue
        sibling = node.module.removeprefix(f"{METHODS_PACKAGE}.")
        pairs.extend((sibling, alias.name) for alias in node.names)
    return pairs


def test_methods_import_graph_has_no_private_sibling_imports() -> None:
    """No module in the family imports an underscore name from a sibling.

    A private name is an implementation detail of the module that defines it.
    Importing one across the package boundary re-couples the modules the split
    separated, so every cross-module name must be public. The pre-split reaches
    in :data:`LEGACY_PRIVATE_SIBLING_IMPORTS` are excluded by exact triple, so a
    new one -- or a legacy one that moves to another module -- still fails.
    """
    offenders: list[str] = []
    for module in _method_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        offenders.extend(
            f"{module.name} imports {name!r} from {sibling}"
            for sibling, name in _sibling_imports(tree)
            if name.startswith("_")
            and (module.name, sibling, name) not in LEGACY_PRIVATE_SIBLING_IMPORTS
        )
    assert offenders == [], f"private sibling imports in the method package: {offenders}"


def test_methods_import_graph_legacy_private_reaches_are_all_still_real() -> None:
    """Every frozen legacy reach still exists, so the allowlist cannot rot.

    Error path for the allowlist itself: once a pair is cleaned up its entry
    must be deleted, otherwise the set would quietly pardon a future reach that
    happens to reuse the same name.
    """
    seen: set[tuple[str, str, str]] = set()
    for module in _method_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        seen.update(
            (module.name, sibling, name)
            for sibling, name in _sibling_imports(tree)
            if name.startswith("_")
        )
    stale = sorted(LEGACY_PRIVATE_SIBLING_IMPORTS - seen)
    assert stale == [], f"legacy private-import allowlist entries no longer exist: {stale}"


def test_methods_import_graph_modules_fit_the_loc_cap() -> None:
    """Every method module is at or under the 1400-line EAWF010 cap.

    Counted the way ``wc -l`` counts, since that is the unit the cap is written
    in. The three pre-split waivered surfaces are exempted by name and no
    others: a new module over the cap fails here.
    """
    oversized: list[str] = []
    for module in _method_modules():
        if module.name in WAIVERED_MODULES:
            continue
        loc = len(module.read_text(encoding="utf-8").splitlines())
        if loc > MAX_LOC:
            oversized.append(f"{module.name} is {loc} lines (cap {MAX_LOC})")
    assert oversized == [], f"method modules over the EAWF010 cap: {oversized}"


def test_methods_import_graph_waivered_modules_still_carry_a_rationale() -> None:
    """Each waivered module carries a real EAWF010 waiver, not a bare mute.

    Boundary on the exemption list itself: a module may only sit in
    :data:`WAIVERED_MODULES` while it actually declares the waiver with prose,
    so the list cannot outlive the waivers it stands for.
    """
    from eawf.platform.lint.eawf010 import find_waiver

    for name in sorted(WAIVERED_MODULES):
        module = METHODS_DIR / name
        assert module.is_file(), f"waivered module is gone: {name}"
        rationale = find_waiver(module.read_text(encoding="utf-8"))
        assert rationale, f"{name} sits on the waiver list with no EAWF010 rationale"


def test_methods_import_graph_has_no_eawf010_exclusion() -> None:
    """No module in the family is exempted by the pyproject EAWF010 exclude list.

    The un-grandfather-on-touch rule: the state router left that list when it
    was split, and nothing in the package may rejoin it.
    """
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    exclude = config["tool"]["eawf"]["lint"]["eawf010"]["exclude"]
    prefix = "src/eawf/runtime/daemon/methods/"
    assert [entry["path"] for entry in exclude if entry["path"].startswith(prefix)] == []


def test_methods_import_graph_is_acyclic_at_module_scope() -> None:
    """The family's module-scope import graph has no cycle.

    Module-scope edges are the ones import order has to satisfy, so a cycle
    among them is a real layering fault rather than a deferred call. Cycles that
    exist only through function-local imports are the deliberate deferral and
    are out of this assertion by construction.
    """
    graph: dict[str, set[str]] = {}
    for module in _method_modules():
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        graph[module.stem] = {
            sibling for sibling, _name in _sibling_imports(tree, module_level_only=True)
        }

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def _walk(node: str, trail: tuple[str, ...]) -> None:
        if node in done:
            return
        if node in visiting:
            cycles.append(" -> ".join((*trail, node)))
            return
        visiting.add(node)
        for neighbour in sorted(graph.get(node, ())):
            _walk(neighbour, (*trail, node))
        visiting.discard(node)
        done.add(node)

    for name in sorted(graph):
        _walk(name, ())
    assert cycles == [], f"import cycles inside the method package: {cycles}"


@pytest.mark.parametrize(
    ("module_name", "symbol"),
    [
        ("state_apply", "build_apply_registry"),
        ("state_apply", "apply_mutation_under_lock"),
        ("state_close", "score_required_criteria"),
        ("state_close", "build_close_attempt_hooks"),
        ("state_close", "enforce_wave_verdict_gate"),
        ("state_close", "compute_wave_close_readiness"),
        ("state_context", "read_state"),
        ("state_events", "mutation_event_extras"),
        ("state_jury", "jury_spawn_factory"),
        ("state_models", "CachedMutation"),
        ("state_runtime", "merge_runtime_latest"),
        ("state_worktree", "commit_worktree_state"),
    ],
)
def test_methods_import_graph_collaborator_owns_its_symbol(module_name: str, symbol: str) -> None:
    """Each named collaborator defines its own public symbol.

    Guards the split's one-reason-to-change property from the other side: a
    symbol that drifts back onto the facade (or onto the wrong collaborator)
    fails here even though the import graph would still be clean.
    """
    module = METHODS_DIR / f"{module_name}.py"
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    assert symbol in defined, f"{symbol} is not defined in {module_name}.py"
