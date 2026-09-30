"""The epoch-1 chassis surfaces are served by the console, and the delete verdicts are honoured.

The design pack judged every epoch-1 TUI surface once: 47 carry to an epoch-2 route, 27
are chassis (a reusable mechanic rather than a surface) and 8 are deleted. The
disposition table records, for each chassis surface, the console or shared-chassis
symbols that now do its job and a console test that exercises them, and whether the
file was kept in the shared chassis or removed with the epoch-1 app.

Each row is realised, not merely listed. A serving symbol must import, must live in the
console or the shared chassis, and must not pull the epoch-1 tree in at import time. A
removed file must be absent and imported by nothing, and nothing under ``src``,
``tools`` or ``tests`` may still import the epoch-1 tree.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from functools import cache
from itertools import combinations
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from textual.theme import Theme

from eawf.surfaces.tui.chassis.asciinema import record_cast
from eawf.surfaces.tui.chassis.cvd import CVD_TYPES, colour_distance, simulate_cvd
from eawf.surfaces.tui.chassis.theme import EA_THEMES, persisted_theme
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.clock import FakeClock
from eawf.surfaces.tui.console.token_map import TOKEN_MAP

REPO = Path(__file__).resolve().parents[5]
SRC = REPO / "src"
TABLE_PATH = REPO / "tests" / "fixtures" / "console" / "chassis-disposition.json"

TUI = "eawf.surfaces.tui"

#: The subpackages under the TUI surface that outlive the epoch-1 app.
SURVIVING = frozenset({"console", "chassis"})

#: The pack's verdict census for the rows this table owns.
CHASSIS = 27
DELETED = 8

#: The launcher that opens the console.
LAUNCH = f"{TUI}.launch"

#: The modules left beside the console and the chassis: the package initialiser, the
#: launcher and the log swap it makes.
ENTRY_MODULES = frozenset({TUI, LAUNCH, f"{TUI}.terminal_logging"})

#: The epoch-1 application and the packages only it mounted.
EPOCH1_TREE = tuple(
    f"{TUI}.{name}"
    for name in (
        "app",
        "attention",
        "modals",
        "modes",
        "palette",
        "scopes",
        "screens",
        "snapshot",
        "toast_emitter",
        "widgets",
    )
)

#: Below this distance two simulated colours read as one; the lifecycle band gate uses
#: the same epsilon.
COLLAPSE_EPSILON = 4.0

#: The console surfaces that carry a severity, beside the word or glyph that also says it.
SEVERITY_SURFACES = ("ok", "info", "warn", "err")


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Disposition(_Closed):
    """One epoch-1 surface and what became of it.

    Attributes:
        surface: The surface as the pack's verdict names it.
        verdict: The pack's verdict.
        function: What the surface did, in one sentence.
        served_by: ``module:attribute`` symbols that now do the job; chassis rows only.
        proofs: ``path::test`` console tests that exercise those symbols; chassis rows only.
        paths: The repository files the surface was, or is, made of.
        fate: What becomes of those files.
        note: Why the fate is what it is, when it is not obvious.
    """

    surface: str
    verdict: Literal["chassis", "delete"]
    function: str
    served_by: tuple[str, ...] = ()
    proofs: tuple[str, ...] = ()
    paths: tuple[str, ...] = Field(min_length=1)
    fate: Literal["kept-in-chassis", "removed"]
    note: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Disposition:
        if self.verdict == "chassis" and not (self.served_by and self.proofs):
            raise ValueError(f"{self.surface}: a chassis row names what serves it and a proof")
        if self.verdict == "delete" and (self.served_by or self.proofs):
            raise ValueError(f"{self.surface}: a deleted surface is served by nothing")
        if self.verdict == "delete" and self.fate == "kept-in-chassis":
            raise ValueError(f"{self.surface}: a deleted surface is not kept")
        return self


class DispositionTable(_Closed):
    """The chassis and delete rows of the pack's epoch-1 verdict.

    Attributes:
        epoch1_app_retired_by: The Task that deletes the epoch-1 app and what retires with it.
        surfaces: One row per judged surface.
    """

    epoch1_app_retired_by: str
    surfaces: tuple[Disposition, ...]


def load_table(path: Path) -> DispositionTable:
    """Return the disposition table at ``path``, validated.

    Raises:
        ValidationError: When a row is malformed or carries an unknown field.
    """
    return DispositionTable.model_validate(json.loads(path.read_text(encoding="utf-8")))


TABLE = load_table(TABLE_PATH)
ROWS = {row.surface: row for row in TABLE.surfaces}
CHASSIS_ROWS = [row for row in TABLE.surfaces if row.verdict == "chassis"]
DELETE_ROWS = [row for row in TABLE.surfaces if row.verdict == "delete"]


def _module_of(path: str) -> str:
    return path.removeprefix("src/").removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def _file_of(module: str) -> Path | None:
    base = SRC.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(module: str, source: Path, *, deep: bool) -> set[str]:
    """Return the modules ``source`` imports.

    Shallow, only statements that run at import time count: module-level imports,
    including those under a top-level ``try``; imports deferred into a function body or
    held under ``TYPE_CHECKING`` are left out. Deep, every import statement counts, so a
    call path that imports lazily is walked too.
    """
    package = module if source.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    tree = ast.parse(source.read_text(encoding="utf-8"))
    stack: list[ast.AST] = list(ast.walk(tree)) if deep else list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Try) and not deep:
            stack.extend(node.body)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join([*parts, node.module] if node.module else parts)
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in found if _file_of(name) is not None}


@cache
def _import_closure(
    roots: tuple[str, ...], stop: frozenset[str] = frozenset(), *, deep: bool = False
) -> frozenset[str]:
    """Return every TUI module importing ``roots`` loads, not walking into ``stop``.

    Deep, the walk also follows imports made inside function bodies.

    The ``eawf.surfaces.tui`` package's own initialiser is left out: it re-exports the
    epoch-1 app, and it goes with that app.
    """
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        module = todo.pop()
        if module in seen or module in stop or not module.startswith(f"{TUI}."):
            continue
        source = _file_of(module)
        if source is None:
            continue
        seen.add(module)
        todo.extend(_imports(module, source, deep=deep))
        parent = module.rpartition(".")[0]
        if parent != TUI:
            todo.append(parent)
    return frozenset(seen)


def _epoch1(modules: frozenset[str]) -> set[str]:
    return {m for m in modules if m.removeprefix(f"{TUI}.").split(".")[0] not in SURVIVING}


def _console_modules() -> tuple[str, ...]:
    root = SRC / "eawf" / "surfaces" / "tui" / "console"
    return tuple(sorted(_module_of(str(p.relative_to(REPO))) for p in root.rglob("*.py")))


def _importers_of(module: str) -> list[str]:
    """Return every source file under ``src`` that imports ``module``, at any depth."""
    hits: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if module.rpartition(".")[2] not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            if module in names:
                hits.append(str(path.relative_to(REPO)))
                break
    return hits


def _resolve(symbol: str) -> object:
    module, _, attribute = symbol.partition(":")
    target: object = importlib.import_module(module)
    for part in attribute.split("."):
        target = getattr(target, part)
    return target


def test_cr_024_the_table_holds_every_chassis_and_delete_verdict_once() -> None:
    assert len(ROWS) == len(TABLE.surfaces)
    assert (len(CHASSIS_ROWS), len(DELETE_ROWS)) == (CHASSIS, DELETED)


@pytest.mark.parametrize("row", CHASSIS_ROWS, ids=lambda row: row.surface)
def test_cr_024_a_chassis_surface_is_served_by_a_console_or_chassis_symbol(
    row: Disposition,
) -> None:
    for symbol in row.served_by:
        module = symbol.partition(":")[0]
        assert module.removeprefix(f"{TUI}.").split(".")[0] in SURVIVING, symbol
        assert _resolve(symbol) is not None
        reached = _epoch1(_import_closure((module,)))
        assert not reached, f"{symbol} pulls in the epoch-1 tree: {sorted(reached)}"


@pytest.mark.parametrize("row", CHASSIS_ROWS, ids=lambda row: row.surface)
def test_cr_024_every_proof_names_a_test_that_exists(row: Disposition) -> None:
    for proof in row.proofs:
        path, _, name = proof.partition("::")
        source = (REPO / path).read_text(encoding="utf-8")
        tests = {
            node.name
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        assert name in tests, proof


@pytest.mark.parametrize(
    "row",
    [row for row in TABLE.surfaces if row.fate == "kept-in-chassis"],
    ids=lambda row: row.surface,
)
def test_cr_024_a_kept_surface_lives_in_the_chassis_clear_of_the_epoch1_tree(
    row: Disposition,
) -> None:
    module = _module_of(row.paths[0])
    assert module.startswith(f"{TUI}.chassis."), row.paths[0]
    assert (REPO / row.paths[0]).is_file()
    assert not _epoch1(_import_closure((module,)))


@pytest.mark.parametrize(
    "row", [row for row in TABLE.surfaces if row.fate == "removed"], ids=lambda row: row.surface
)
def test_cr_024_a_removed_surface_is_gone_and_imported_by_nothing(row: Disposition) -> None:
    for path in row.paths:
        assert not (REPO / path).exists(), path
        assert _importers_of(_module_of(path)) == [], path


def test_cr_024_the_console_entry_reaches_no_epoch1_module() -> None:
    roots = (LAUNCH, *_console_modules())
    entry = _import_closure(roots, deep=True)
    assert _epoch1(entry) <= ENTRY_MODULES


def _epoch1_imports(path: Path, *, strings: bool) -> list[str]:
    """Return every epoch-1 module ``path`` imports, and names as a dotted string.

    Source and tooling name a module in a string to load it later, so a string counts
    there; a test names one to assert it stays unloaded, so ``strings`` is off for tests.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.extend([node.module, *(f"{node.module}.{a.name}" for a in node.names)])
        elif strings and isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.append(node.value.partition(":")[0])
    return [
        name
        for name in names
        if any(name == root or name.startswith(f"{root}.") for root in EPOCH1_TREE)
    ]


def test_cr_026_the_epoch1_tree_is_gone_and_nothing_imports_it() -> None:
    for module in EPOCH1_TREE:
        assert _file_of(module) is None, module
    offenders = {
        str(path.relative_to(REPO)): found
        for root in ("src", "tools", "tests")
        for path in (REPO / root).rglob("*.py")
        if (found := _epoch1_imports(path, strings=root != "tests"))
    }
    assert offenders == {}


def test_cr_026_the_scan_sees_an_import_of_the_epoch1_tree(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from eawf.surfaces.tui.app import EaApp\n"
        "import eawf.surfaces.tui.widgets.footer\n"
        "TARGET = 'eawf.surfaces.tui.modes.nav:go'\n"
        "KEPT = 'eawf.surfaces.tui.console.app'\n",
        encoding="utf-8",
    )
    imports = ["eawf.surfaces.tui.app", "eawf.surfaces.tui.app.EaApp"]
    imports.append("eawf.surfaces.tui.widgets.footer")
    assert _epoch1_imports(probe, strings=False) == imports
    assert _epoch1_imports(probe, strings=True) == [*imports, "eawf.surfaces.tui.modes.nav"]


def test_cr_024_the_persisted_theme_is_read_by_the_chassis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "eawf.kernel.config.layered.global_config_path", lambda: tmp_path / "absent.yaml"
    )
    config = tmp_path / ".ea" / "config.yaml"
    config.parent.mkdir()
    config.write_text("ui:\n  theme: cb\n", encoding="utf-8")
    assert persisted_theme(tmp_path) == "cb"
    config.write_text("ui:\n  theme: sepia\n", encoding="utf-8")
    assert persisted_theme(tmp_path) == "dark"


def test_cr_024_a_cast_records_the_console() -> None:
    async def frames() -> list[tuple[float, str]]:
        app = ConsoleApp(clock=FakeClock())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            return await record_cast(
                pilot, [("press", "question_mark"), ("pause", "0")], frame_ms=500
            )

    cast = asyncio.run(frames())
    assert [ts for ts, _ in cast] == pytest.approx([0.0, 0.5, 1.0])
    assert "Eä" in cast[0][1]
    assert cast[1][1] != cast[0][1]


@pytest.mark.parametrize("theme", EA_THEMES, ids=lambda theme: theme.name)
@pytest.mark.parametrize("cvd_type", CVD_TYPES)
def test_cr_024_the_console_severity_colours_stay_apart_for_a_dichromat(
    theme: Theme, cvd_type: str
) -> None:
    tokens = {row.surface: row.token for row in TOKEN_MAP if row.surface in SEVERITY_SURFACES}
    seen = {name: simulate_cvd(theme.variables[token], cvd_type) for name, token in tokens.items()}
    assert set(seen) == set(SEVERITY_SURFACES)
    for a, b in combinations(seen, 2):
        assert colour_distance(seen[a], seen[b]) > COLLAPSE_EPSILON, (a, b)


def test_cr_024_a_row_with_an_unknown_field_is_refused(tmp_path: Path) -> None:
    raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    raw["surfaces"][0]["owner"] = "nobody"
    bad = tmp_path / "table.json"
    bad.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValidationError, match="owner"):
        load_table(bad)


def test_cr_024_a_chassis_row_naming_nothing_that_serves_it_is_refused() -> None:
    with pytest.raises(ValidationError, match="names what serves it"):
        Disposition(
            surface="x.py", verdict="chassis", function="f", paths=("x.py",), fate="removed"
        )


def test_cr_024_a_deleted_row_that_claims_to_be_kept_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not kept"):
        Disposition(
            surface="x.py", verdict="delete", function="f", paths=("x.py",), fate="kept-in-chassis"
        )
